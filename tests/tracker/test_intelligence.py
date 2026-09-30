from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.tracker.intelligence import EnrichmentService


def _html(
    title: str,
    *,
    description: str = "Finance or Economics degree. Applicants must graduate in 2028. Sponsorship available. 2:1 required.",
    location: str = "London, United Kingdom",
    apply: bool = True,
    date_posted: str | None = "2026-09-01",
    valid_through: str | None = "2027-01-15",
) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": description,
        "hiringOrganization": {"name": "Example Bank"},
        "jobLocation": {"address": {"addressLocality": location}},
        "url": "https://jobs.example.test/roles/detail",
    }
    if date_posted is not None:
        posting["datePosted"] = date_posted
    if valid_through is not None:
        posting["validThrough"] = valid_through
    button = '<a class="apply-button" href="/apply">Apply now</a>' if apply else ""
    return (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(posting)
        + "</script></head><body><main>"
        + f"<h1>{title}</h1><p>{description}</p><p>Deadline: 15 January 2027</p>{button}"
        + "</main></body></html>"
    )


def _jobs(*titles: str) -> list[dict]:
    return [
        {
            "id": index + 1,
            "employer": "Example Bank",
            "title": title,
            "url": f"https://jobs.example.test/roles/{index + 1}",
            "location": "London, United Kingdom",
            "programme": "summer",
        }
        for index, title in enumerate(titles)
    ]


def test_run_decorates_verified_open_role_and_persists_hash_without_html(tmp_path: Path) -> None:
    job = _jobs("Summer Finance Analyst Internship 2028")[0]
    calls: list[str] = []

    def fetch(url: str, **_: object) -> dict:
        calls.append(url)
        return {"status_code": 200, "final_url": url, "body": _html(job["title"])}

    service = EnrichmentService(tmp_path, fetcher=fetch, max_workers=2)
    outcome = service.run([job])
    decorated = service.decorate([job])[0]

    assert outcome["checked"] == 1
    assert calls == [job["url"]]
    assert decorated["availability"] == "open"
    assert decorated["verified_at"]
    assert decorated["deadline"] == "2027-01-15"
    assert decorated["deadline_text"] == "15 January 2027"
    assert decorated["posted_at"] == "2026-09-01T00:00:00+00:00"
    assert decorated["posted_text"] == "2026-09-01"
    assert decorated["match_status"] == "review"  # Employer criteria do not establish the applicant meets them.
    assert decorated["match_reasons"]
    assert decorated["evidence"]
    assert all("html" not in item and "body" not in item for item in decorated.values() if isinstance(item, dict))
    assert "body" not in decorated
    assert decorated["source_response_hash"]
    assert service.summary()["open"] == 1


def test_cache_survives_reopen_and_does_not_refetch_healthy_result(tmp_path: Path) -> None:
    job = _jobs("Summer Finance Analyst Internship 2028")[0]
    first_calls: list[str] = []
    first = EnrichmentService(
        tmp_path,
        fetcher=lambda url, **kwargs: first_calls.append(url)
        or {"status_code": 200, "final_url": url, "body": _html(job["title"])},
    )
    assert first.run([job])["checked"] == 1

    second_calls: list[str] = []
    second = EnrichmentService(
        tmp_path,
        fetcher=lambda url, **kwargs: second_calls.append(url) or (_ for _ in ()).throw(AssertionError()),
    )

    assert second.run([job])["checked"] == 0
    assert second.decorate([job])[0]["availability"] == "open"
    assert first_calls == [job["url"]]
    assert second_calls == []


def test_bounded_batch_keeps_unchecked_roles_queued_for_next_run(tmp_path: Path) -> None:
    jobs = _jobs(
        "Summer Finance Analyst Internship 2028",
        "Summer Risk Analyst Internship 2028",
        "Summer Markets Internship 2028",
    )
    calls: list[str] = []

    def fetch(url: str, **_: object) -> dict:
        calls.append(url)
        title = next(job["title"] for job in jobs if job["url"] == url)
        return {"status_code": 200, "final_url": url, "body": _html(title)}

    service = EnrichmentService(tmp_path, fetcher=fetch, max_batch=2)
    assert service.run(jobs)["checked"] == 2
    assert service.run(jobs)["checked"] == 1
    assert len(calls) == 3
    assert set(calls) == {job["url"] for job in jobs}
    assert service.run(jobs)["checked"] == 0


def test_http_errors_and_shells_never_become_open(tmp_path: Path) -> None:
    jobs = _jobs(
        "Summer Finance Analyst Internship 2028",
        "Summer Risk Analyst Internship 2028",
        "Summer Markets Internship 2028",
        "Summer Quant Internship 2028",
    )
    by_title = {job["title"]: job for job in jobs}

    def fetch(url: str, **_: object) -> dict:
        title = next(job["title"] for job in jobs if job["url"] == url)
        if "Finance" in title:
            return {"status_code": 404, "final_url": url, "body": "not found"}
        if "Risk" in title:
            return {"status_code": 410, "final_url": url, "body": "gone"}
        if "Markets" in title:
            return {"status_code": 200, "final_url": url, "body": "<html><body>Access denied; verify you are human</body></html>"}
        return {
            "status_code": 200,
            "final_url": "https://example.test/careers",
            "body": "<html><body><h1>Careers</h1><nav>Jobs</nav></body></html>",
        }

    service = EnrichmentService(tmp_path, fetcher=fetch, max_batch=10)
    service.run(jobs)
    decorated = {job["title"]: row for job, row in zip(jobs, service.decorate(jobs))}

    assert decorated[jobs[0]["title"]]["availability"] == "closed"
    assert decorated[jobs[1]["title"]]["availability"] == "closed"
    assert decorated[jobs[2]["title"]]["availability"] == "unknown"
    assert decorated[jobs[3]["title"]]["availability"] == "unknown"
    assert all(row["availability"] != "open" for row in decorated.values())
    assert by_title[jobs[0]["title"]]["title"] == "Summer Finance Analyst Internship 2028"


def test_explicit_stem_masters_and_wrong_graduation_year_are_excluded(tmp_path: Path) -> None:
    job = _jobs("Summer Technology Internship 2028")[0]
    description = (
        "Computer Science, Engineering or other STEM degree required. "
        "Master's degree required. Applicants must graduate in 2027."
    )

    service = EnrichmentService(
        tmp_path,
        fetcher=lambda url, **kwargs: {
            "status_code": 200,
            "final_url": url,
            "body": _html(job["title"], description=description),
        },
    )
    service.run([job])
    decorated = service.decorate([job])[0]

    assert decorated["match_status"] == "excluded"
    assert "requires_stem_degree" in decorated["match_reasons"]
    assert "requires_masters" in decorated["match_reasons"]
    assert "graduation_year_mismatch" in decorated["match_reasons"]
    assert decorated["availability"] == "open"


def test_profile_edit_re_evaluates_cached_facts_without_network_call(tmp_path: Path) -> None:
    job = _jobs("Summer Finance Analyst Internship 2028")[0]
    calls: list[str] = []
    service = EnrichmentService(
        tmp_path,
        fetcher=lambda url, **kwargs: calls.append(url)
        or {"status_code": 200, "final_url": url, "body": _html(job["title"])},
    )
    service.run([job])
    service.set_profile(
        {
            "graduation_years": {"summer": 2027, "year_in_industry": 2029, "spring_week": 2029},
            "degree": "Finance",
            "desired_roles": ["summer"],
            "desired_locations": ["UK"],
        }
    )

    decorated = service.decorate([job])[0]
    assert calls == [job["url"]]
    assert decorated["match_status"] == "excluded"
    assert "graduation_year_mismatch" in decorated["match_reasons"]


def test_private_job_url_is_recorded_unknown_without_calling_injected_fetcher(tmp_path: Path) -> None:
    job = _jobs("Summer Finance Analyst Internship 2028")[0]
    job["url"] = "http://127.0.0.1/private"
    calls: list[str] = []

    service = EnrichmentService(
        tmp_path,
        fetcher=lambda url, **kwargs: calls.append(url) or {"availability": "open"},
    )

    service.run([job])
    decorated = service.decorate([job])[0]

    assert calls == []
    assert decorated["availability"] == "unknown"
    assert "private" in decorated["verification_error"].lower()


def test_healthy_and_error_cache_windows_are_bounded(tmp_path: Path) -> None:
    good_job = _jobs("Summer Finance Analyst Internship 2028")[0]
    bad_job = _jobs("Summer Risk Analyst Internship 2028")[0]
    bad_job["id"] = 99
    bad_job["url"] = "https://jobs.example.test/roles/99"

    def fetch(url: str, **kwargs: object) -> dict:
        if url == good_job["url"]:
            return {"status_code": 200, "final_url": url, "body": _html(good_job["title"])}
        raise RuntimeError("fixture failure")

    service = EnrichmentService(tmp_path, fetcher=fetch)
    service.run([good_job, bad_job])
    with sqlite3.connect(tmp_path / "enrichment.sqlite3") as connection:
        windows = {
            row[0]: row[1] - row[2]
            for row in connection.execute(
                "SELECT job_key, next_due_epoch, last_checked_epoch FROM refresh_queue"
            )
        }

    assert windows["id:1"] <= 12 * 60 * 60
    assert windows["id:99"] <= 60 * 60
