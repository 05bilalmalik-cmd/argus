from pathlib import Path

import pytest

from app.tracker.contracts import Listing, SourceResult
from app.tracker.store import TrackerStore


SOURCE_URL = "https://sources.example.test/greenhouse"
GENERIC_URL = "https://jobs.example.test/careers?query=placement"


def listing(
    *,
    title="Analyst Placement",
    url="https://boards.greenhouse.io/acme/jobs/1001",
    source_id="1001",
    employer="Acme",
    location="London",
    programme="placement",
    posted_at="2026-09-01T10:00:00+00:00",
    deadline="2026-10-01",
):
    return Listing(
        employer=employer,
        title=title,
        url=url,
        location=location,
        programme=programme,
        source_id=source_id,
        posted_at=posted_at,
        deadline=deadline,
    )


def result(
    *listings,
    name="greenhouse",
    url=SOURCE_URL,
    status="ok",
    checked_at="2026-09-01T12:00:00+00:00",
    error="",
):
    return SourceResult(
        name=name,
        url=url,
        status=status,
        listings=list(listings),
        error=error,
        checked_at=checked_at,
        elapsed_seconds=0.25,
    )


def test_insert_dedup_metadata_and_user_fields_survive_reopen(tmp_path: Path):
    path = tmp_path / "tracker.sqlite3"
    store = TrackerStore(path)

    first = store.ingest([result(listing())])
    assert first == {"new": 1, "updated": 0, "observed": 1, "sources": 1, "errors": 0}
    job = store.list_jobs()[0]
    job_id = job["id"]
    store.update_job(
        job_id,
        saved=True,
        stage="applied",
        notes="Keep the assessment link",
        due_date="2026-09-20",
    )

    second = store.ingest(
        [
            result(
                listing(
                    title="Senior Analyst Placement",
                    location="London / hybrid",
                    programme="year_in_industry",
                    deadline="2026-10-15",
                ),
                checked_at="2026-09-02T12:00:00Z",
            )
        ]
    )
    assert second == {"new": 0, "updated": 1, "observed": 1, "sources": 1, "errors": 0}
    job = store.list_jobs()[0]
    assert job["id"] == job_id
    assert job["title"] == "Senior Analyst Placement"
    assert job["location"] == "London / hybrid"
    assert job["deadline"] == "2026-10-15"
    assert job["first_seen"] == "2026-09-01T12:00:00+00:00"
    assert job["last_seen"] == "2026-09-02T12:00:00+00:00"
    assert job["saved"] is True
    assert job["stage"] == "applied"
    assert job["notes"] == "Keep the assessment link"
    assert job["due_date"] == "2026-09-20"

    reopened = TrackerStore(path)
    assert reopened.list_jobs() == store.list_jobs()
    assert reopened.list_sources()[0]["last_success_at"] == "2026-09-02T12:00:00+00:00"


def test_ats_variants_and_exact_direct_urls_merge_but_generic_roles_do_not(tmp_path: Path):
    store = TrackerStore(tmp_path / "tracker.sqlite3")
    store.ingest([result(listing())])
    store.ingest(
        [
            result(
                listing(
                    title="Analyst Placement (updated)",
                    url="https://job-boards.greenhouse.io/acme/jobs/1001?gh_jid=1001",
                    source_id="",
                ),
                name="greenhouse-copy",
                checked_at="2026-09-01T13:00:00+00:00",
            )
        ]
    )
    store.ingest(
        [
            result(
                listing(
                    title="Analyst Placement (apply)",
                    url="https://boards.greenhouse.io/embed/job_app?for=acme&token=1001",
                    source_id="",
                ),
                name="greenhouse-apply",
                checked_at="2026-09-01T13:30:00+00:00",
            )
        ]
    )
    store.ingest(
        [
            result(
                listing(
                    title="Risk Internship",
                    url="https://careers.example.test/jobs/42",
                    source_id="",
                ),
                name="other-source",
                checked_at="2026-09-01T14:00:00+00:00",
            ),
            result(
                listing(
                    title="Engineering Internship",
                    url=GENERIC_URL,
                    source_id="",
                ),
                name="generic-source",
                checked_at="2026-09-01T14:00:00+00:00",
            ),
        ]
    )
    store.ingest(
        [
            result(
                listing(
                    title="Risk Internship - refreshed",
                    url="https://careers.example.test/jobs/42",
                    source_id="",
                ),
                name="other-copy",
                checked_at="2026-09-01T15:00:00+00:00",
            ),
            result(
                listing(
                    title="Finance Internship",
                    url=GENERIC_URL,
                    source_id="",
                ),
                name="generic-copy",
                checked_at="2026-09-01T15:00:00+00:00",
            ),
        ]
    )

    jobs = store.list_jobs()
    assert len(jobs) == 4
    assert len(next(job for job in jobs if "careers.example.test/jobs/42" in job["url"])["sources"]) == 2
    assert {job["title"] for job in jobs if job["url"] == GENERIC_URL} == {
        "Engineering Internship",
        "Finance Internship",
    }
    greenhouse = next(job for job in jobs if "greenhouse" in job["url"] or "job-boards" in job["url"])
    assert set(greenhouse["sources"]) == {
        "greenhouse",
        "greenhouse-apply",
        "greenhouse-copy",
    }


def test_named_source_alias_keeps_identity_across_url_and_metadata_changes(tmp_path: Path):
    store = TrackerStore(tmp_path / "tracker.sqlite3")
    store.ingest(
        [
            result(
                listing(
                    title="Operations Placement",
                    url="https://structured.example.test/search?query=ops",
                    source_id="stable-77",
                    location="Leeds",
                ),
                name="structured",
                checked_at="2026-09-01T10:00:00+00:00",
            )
        ]
    )
    store.ingest(
        [
            result(
                listing(
                    title="Operations Placement (revised)",
                    url="https://structured.example.test/careers?query=ops-2027",
                    source_id="stable-77",
                    location="Manchester",
                ),
                name="structured",
                checked_at="2026-09-02T10:00:00+00:00",
            )
        ]
    )
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Operations Placement (revised)"
    assert jobs[0]["location"] == "Manchester"
    assert jobs[0]["url"].endswith("query=ops-2027")


def test_failure_retains_roles_and_last_success_partial_ingests_stale_does_not_regress(
    tmp_path: Path,
):
    store = TrackerStore(tmp_path / "tracker.sqlite3")
    store.ingest([result(listing(), checked_at="2026-09-05T12:00:00+00:00")])

    failed = store.ingest(
        [
            result(
                name="greenhouse",
                status="error",
                checked_at="2026-09-05T13:00:00+00:00",
                error="HTTP 503",
            )
        ]
    )
    assert failed["errors"] == 1
    source = store.list_sources()[0]
    assert source["status"] == "error"
    assert source["error"] == "HTTP 503"
    assert source["last_success_at"] == "2026-09-05T12:00:00+00:00"
    assert len(store.list_jobs()) == 1

    partial = store.ingest(
        [
            result(
                listing(
                    title="New Partial Role",
                    url="https://jobs.example.test/role/new",
                    source_id="new-partial",
                ),
                status="partial",
                checked_at="2026-09-05T14:00:00+00:00",
                error="pagination stopped",
            )
        ]
    )
    assert partial["observed"] == 1
    assert partial["errors"] == 0
    assert store.list_sources()[0]["status"] == "partial"
    assert store.list_sources()[0]["last_success_at"] == "2026-09-05T12:00:00+00:00"

    stale = store.ingest(
        [
            result(
                listing(title="Old title", deadline="2026-09-01"),
                checked_at="2026-09-04T12:00:00+00:00",
            )
        ]
    )
    assert stale["updated"] == 1
    original = next(job for job in store.list_jobs() if job["id"] == 1)
    assert original["title"] == "Analyst Placement"
    assert original["deadline"] == "2026-10-01"
    assert original["last_seen"] == "2026-09-05T12:00:00+00:00"
    assert store.list_sources()[0]["checked_at"] == "2026-09-05T14:00:00+00:00"
    assert store.summary()["source_errors"] == 0


def test_malformed_source_is_atomic_and_other_sources_are_kept(tmp_path: Path):
    store = TrackerStore(tmp_path / "tracker.sqlite3")
    outcome = store.ingest(
        [
            result(
                Listing("Good Employer", "Good Role", "https://good.example.test/jobs/1"),
                name="good",
            ),
            result(
                Listing("", "Missing Employer", "https://bad.example.test/jobs/1"),
                name="bad",
            ),
        ]
    )
    assert outcome["new"] == 1
    assert outcome["observed"] == 1
    assert outcome["errors"] == 1
    assert len(store.list_jobs()) == 1
    sources = {source["name"]: source for source in store.list_sources()}
    assert sources["good"]["status"] == "ok"
    assert sources["bad"]["status"] == "error"
    assert "employer" in sources["bad"]["error"]


def test_invalid_inputs_and_update_validation(tmp_path: Path):
    store = TrackerStore(tmp_path / "tracker.sqlite3")
    with pytest.raises(ValueError):
        store.ingest([result(status="unknown")])
    with pytest.raises(ValueError):
        store.ingest([result(checked_at="not-a-date")])
    unsafe = store.ingest(
        [
            result(
                Listing("Acme", "Role", "http://127.0.0.1/job/1"),
                name="unsafe",
            )
        ]
    )
    assert unsafe["errors"] == 1
    assert store.list_sources()[0]["status"] == "error"
    credentials = store.ingest(
        [
            result(
                Listing("Acme", "Role", "https://user:password@example.test/job/1"),
                name="credentials",
            )
        ]
    )
    assert credentials["errors"] == 1
    assert {source["name"] for source in store.list_sources()} == {"credentials", "unsafe"}
    with pytest.raises(ValueError):
        store.ingest([result(name="invalid-source-url", url="http://localhost/source")])

    store.ingest([result(listing())])
    job_id = store.list_jobs()[0]["id"]
    with pytest.raises(KeyError):
        store.update_job(9999, saved=True)
    with pytest.raises(ValueError):
        store.update_job(job_id, stage="submitted")
    with pytest.raises(ValueError):
        store.update_job(job_id, due_date="tomorrow")
    cleared = store.update_job(job_id, due_date="")
    assert cleared["due_date"] is None
    assert store.summary()["saved"] == 0
