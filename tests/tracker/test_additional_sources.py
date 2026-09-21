from __future__ import annotations

import threading
import time

import pytest

from app.tracker import additional_sources
from app.tracker.contracts import Listing, SourceResult


def _listing_payload(*, path: str, title: str, req_id: str) -> dict:
    return {
        "title": title,
        "externalPath": path,
        "locationsText": "London, United Kingdom",
        "jobPostingId": req_id,
        "bulletFields": ["Investment Management", "Early Careers"],
    }


def test_seed_url_derives_exact_workday_tenant_site_without_guessing():
    config = additional_sources.derive_workday_board(
        "https://blackstone.wd1.myworkdayjobs.com/en-US/Blackstone_Campus_Careers/job/London/Role_43179"
    )

    assert config.tenant == "blackstone"
    assert config.site == "Blackstone_Campus_Careers"
    assert config.host == "blackstone.wd1.myworkdayjobs.com"
    assert config.jobs_url.endswith("/wday/cxs/blackstone/Blackstone_Campus_Careers/jobs")

    with pytest.raises(ValueError):
        additional_sources.derive_workday_board(
            "https://blackstone.wd1.myworkdayjobs.com/en-US/Blackstone_Campus_Careers/jobs"
        )


def test_workday_parser_filters_graduate_and_technical_roles_and_keeps_ids():
    payload = {
        "total": 4,
        "jobPostings": [
            _listing_payload(
                path="/job/London/2027-Asset-Management-Summer-Analyst_43179",
                title="2027 Asset Management Summer Analyst",
                req_id="43179",
            ),
            _listing_payload(
                path="/job/London/Software-Engineering-Intern_R1",
                title="Software Engineering Intern",
                req_id="R1",
            ),
            _listing_payload(
                path="/job/London/2027-Graduate-Programme_R2",
                title="2027 Graduate Programme",
                req_id="R2",
            ),
            _listing_payload(
                path="/job/London/Marketing-Intern_R3",
                title="Marketing Internship",
                req_id="R3",
            ),
        ],
    }

    report = additional_sources.parse_workday_payload_report(
        payload,
        additional_sources.derive_workday_board(
            "https://blackstone.wd1.myworkdayjobs.com/en-US/Blackstone_Campus_Careers/job/London/Role_43179"
        ),
    )

    assert [item.title for item in report.listings] == [
        "2027 Asset Management Summer Analyst"
    ]
    assert report.listings[0].source_id == "workday:blackstone:43179"
    assert report.filtered_records == 3
    assert report.listings[0].programme == "summer"


def test_workday_detail_metadata_preserves_source_text_and_never_uses_updated_at():
    board = additional_sources.derive_workday_board(
        "https://mufgub.wd3.myworkdayjobs.com/en-US/MUFG-EarlyCareers/job/Role_10079259"
    )
    listing = additional_sources.parse_workday_detail(
        {
            "jobPostingInfo": {
                "title": "2027 Summer Analyst Programme: Capital Markets",
                "jobPostingId": "10079259-WD",
                "externalPath": "/job/Role_10079259",
                "location": "London, United Kingdom",
                "jobDescription": "Employer description for the capital markets role.",
                "startDate": "2027-06-01",
                "endDate": "2027-08-31",
                "postedOn": "2026-09-05",
                "updatedAt": "2026-09-11T12:00:00Z",
                "applicationDeadline": "30 September 2026",
            }
        },
        board,
    )

    assert listing is not None
    assert listing.description == "Employer description for the capital markets role."
    assert listing.source_id == "workday:mufgub:10079259-WD"
    assert listing.posted_at == "2026-09-05T00:00:00+00:00"
    assert listing.posted_text == "2026-09-05"
    assert listing.deadline == "2026-09-30"
    assert listing.deadline_text == "30 September 2026"
    assert "2026-09-11" not in listing.posted_text


def test_collect_additional_sources_paginates_dedupes_and_merges_detail(monkeypatch):
    board = additional_sources.derive_workday_board(
        "https://citi.wd5.myworkdayjobs.com/en-US/2/job/London/Role_26993182"
    )
    monkeypatch.setattr(additional_sources, "WORKDAY_BOARDS", (board,))
    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, payload: dict) -> dict:
        calls.append((url, payload))
        if payload["offset"] == 0:
            return {
                "total": 21,
                "jobPostings": [
                    _listing_payload(
                        path="/job/London/Banking-Summer-Analyst_26993182",
                        title="2027 Banking Summer Analyst",
                        req_id="26993182",
                    ),
                    _listing_payload(
                        path="/job/London/Banking-Summer-Analyst_26993182",
                        title="2027 Banking Summer Analyst",
                        req_id="26993182",
                    ),
                ],
            }
        return {
            "total": 21,
            "jobPostings": [
                _listing_payload(
                    path="/job/London/Software-Engineering-Intern_R2",
                    title="Software Engineering Intern",
                    req_id="R2",
                )
            ],
        }

    def fake_get(url: str) -> dict:
        return {
            "jobPostingInfo": {
                "title": "2027 Banking Summer Analyst",
                "jobPostingId": "26993182",
                "externalPath": "/job/London/Banking-Summer-Analyst_26993182",
                "locationsText": "London, United Kingdom",
                "jobDescription": "Banking employer description.",
            }
        }

    monkeypatch.setattr(additional_sources, "http_post_json", fake_post)
    monkeypatch.setattr(additional_sources, "http_get_json", fake_get)

    results = additional_sources.collect_additional_sources()

    assert len(results) == 1
    assert results[0].status == "ok"
    assert len(results[0].listings) == 1
    assert results[0].listings[0].description == "Banking employer description."
    assert any(payload.get("offset") == 0 for url, payload in calls if url.endswith("/jobs"))
    assert any(payload.get("offset") == 20 for url, payload in calls if url.endswith("/jobs"))


def test_collection_keeps_valid_rows_and_reports_partial_detail_or_page_failure(monkeypatch):
    board = additional_sources.derive_workday_board(
        "https://wf.wd1.myworkdayjobs.com/en-US/WellsFargoJobs/job/CITY-OF-LONDON/Role_R-570663"
    )
    monkeypatch.setattr(additional_sources, "WORKDAY_BOARDS", (board,))
    calls = 0

    def fake_post(url: str, payload: dict) -> dict:
        nonlocal calls
        calls += 1
        return {
            "total": 1,
            "jobPostings": [
                _listing_payload(
                    path="/job/CITY-OF-LONDON/EMEA-Banking-Summer-Analyst_R-570654-1",
                    title="EMEA Banking Summer Analyst",
                    req_id="R-570654-1",
                )
            ],
        }

    def fake_get(url: str) -> dict:
        raise TimeoutError("detail timed out")

    monkeypatch.setattr(additional_sources, "http_post_json", fake_post)
    monkeypatch.setattr(additional_sources, "http_get_json", fake_get)

    result = additional_sources.collect_additional_sources()[0]

    assert result.status == "partial"
    assert len(result.listings) == 1
    assert result.listings[0].description == ""
    assert "detail" in result.error.lower()
    assert calls >= 1


def test_collection_uses_no_more_than_three_workers(monkeypatch):
    boards = tuple(
        additional_sources.derive_workday_board(
            f"https://firm{index}.wd3.myworkdayjobs.com/en-US/Site/job/Role_{index}"
        )
        for index in range(6)
    )
    monkeypatch.setattr(additional_sources, "WORKDAY_BOARDS", boards)
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_post(url: str, payload: dict) -> dict:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        if url.endswith("/jobs"):
            return {"total": 0, "jobPostings": []}
        return {"jobPostingInfo": {}}

    monkeypatch.setattr(additional_sources, "http_post_json", fake_post)

    results = additional_sources.collect_additional_sources()

    assert len(results) == len(boards)
    assert peak <= 3
    assert all(result.status == "empty" for result in results)


def test_source_result_contract_is_data_only():
    result = additional_sources._error_result(
        board=additional_sources.derive_workday_board(
            "https://hl.wd1.myworkdayjobs.com/en-US/Campus/job/Role_R3564"
        ),
        error="HTTP 403",
        started=0.0,
    )

    assert isinstance(result, SourceResult)
    assert result.listings == []
    assert result.status == "error"
    assert all(isinstance(item, Listing) for item in result.listings)
