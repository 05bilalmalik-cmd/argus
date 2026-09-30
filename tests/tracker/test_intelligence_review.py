from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from app.tracker import public_web
from app.tracker.intelligence import EnrichmentService
from app.tracker.profile import ProfileValidationError, validate_profile
from app.tracker.public_web import (
    PublicWebClient,
    PublicWebError,
    ResolvedURL,
    parse_job_page,
)


# Public evidence quotes copied from .hermes/argus-v2/enrichment-live.json.
# These are fixture labels, not live fetches. They preserve the parent-observed
# HTML/date artifacts so this review remains cheap, deterministic, and public-only.
PARENT_LIVE_BLACKROCK_URL = (
    "https://careers.blackrock.com/job/london/2027-placement-program-emea/45831/97150826704"
)
PARENT_LIVE_BLACKROCK_DESCRIPTION_HTML = (
    "<p>BlackRock offers placement opportunities for a duration of 12 months. "
    "This programme is for candidates graduating in 2029 who are required by their "
    "degree to complete a long-term internship.</p><p>This opportunity gives you a "
    "chance to work closely with and add value to a specific team, set and complete "
    "objectives, and be exposed to BlackRock’s culture and ethos. During the placement, "
    "you will develop new skills and gain valuable experience within the asset management"
)
PARENT_LIVE_AIRBUS_DESCRIPTION_QUOTE = (
    "Job Description: Start date: 16 August 2027 Location: Filton, Bristol Duration: "
    "12.5 months Applications close on 27 September 2026! We love your interest in "
    "joining Airbus! There is no limit on the number of positions you can apply for, "
    "however, please be aware that you can only progress in the selection process for "
    "one position at a time."
)
PARENT_LIVE_SMBC_DESCRIPTION_HTML = (
    "<h3><span>What is the Opportunity?</span></h3><p><span>SMBC hosts a 12-month "
    "Industrial Placement Programme for penultimate year students who will have the "
    "opportunity to work on teams, be exposed to significant firm-wide projects "
    "within their respective business areas and support a range of deals with our "
    "clients.</span></p>"
)
PARENT_LIVE_BLACKROCK_DEADLINE_ARTIFACT = (
    "Deadline: ly with and add value to a specific team, set and complete objectives, "
    "and be exposed to BlackRock’s"
)
PARENT_LIVE_QUEUE_RESULT = {"checked": 60, "queued": 60, "errors": 43, "open": 17, "unknown": 43}
PARENT_LIVE_QUEUE_SUMMARY = {"total": 60, "queued": 244, "open": 17, "unknown": 43}


TEST_PROFILE: dict[str, object] = {
    "graduation_years": {
        "summer": 2028,
        "year_in_industry": 2029,
        "spring_week": 2029,
    },
    "degree": "Finance",
    "desired_roles": ["summer", "year_in_industry", "spring_week"],
    "desired_locations": ["UK"],
}


def _job(
    *,
    job_id: object = 1,
    title: str = "Summer Finance Analyst Internship 2028",
    employer: str = "Example Bank",
    url: str | None = None,
    location: str = "London, United Kingdom",
    programme: str = "summer",
    **extra: object,
) -> dict[str, object]:
    job = {
        "id": job_id,
        "employer": employer,
        "title": title,
        "url": url or f"https://jobs.example.test/roles/{job_id}",
        "location": location,
        "programme": programme,
    }
    job.update(extra)
    return job


def _facts_fetcher(
    job: dict[str, object],
    role_text: str,
    **overrides: object,
):
    facts: dict[str, object] = {
        "availability": "open",
        "verification_error": "",
        "title": job["title"],
        "employer": job["employer"],
        "location": job["location"],
        "role_text": role_text,
        "evidence": [
            {
                "quote": role_text[:500],
                "source_url": job["url"],
                "observed_at": "2026-09-11T12:00:00+00:00",
            }
        ],
    }
    facts.update(overrides)
    return lambda _url, **_kwargs: dict(facts)


def _run_match(
    tmp_path: Path,
    job: dict[str, object],
    role_text: str,
    **facts: object,
) -> dict[str, object]:
    service = EnrichmentService(
        tmp_path,
        profile=copy.deepcopy(TEST_PROFILE),
        fetcher=_facts_fetcher(job, role_text, **facts),
    )
    assert service.run([job])["checked"] == 1
    return service.decorate([job])[0]


def _role_page(
    title: str,
    body: str,
    *,
    apply: bool = True,
    jsonld: dict[str, Any] | None = None,
) -> str:
    structured = ""
    if jsonld is not None:
        structured = (
            '<script type="application/ld+json">'
            + json.dumps(jsonld, ensure_ascii=False)
            + "</script>"
        )
    apply_markup = '<a href="/apply">Apply</a>' if apply else ""
    return (
        "<html><head>"
        + structured
        + "</head><body><main>"
        + f"<h1>{title}</h1><p>{body}</p>{apply_markup}"
        + "</main></body></html>"
    )


def test_parent_blackrock_programme_year_is_not_a_graduation_requirement(
    tmp_path: Path,
) -> None:
    # Parent live role id 300: the 2027 title is the placement cycle, while the
    # quoted eligibility text says candidates graduate in 2029.
    job = _job(
        job_id=300,
        title="2027 Placement Program - EMEA",
        employer="BlackRock",
        url=PARENT_LIVE_BLACKROCK_URL,
        programme="year_in_industry",
    )
    row = _run_match(tmp_path, job, PARENT_LIVE_BLACKROCK_DESCRIPTION_HTML)

    assert row["availability"] == "open"
    assert row["match_status"] != "excluded", row
    assert "graduation_year_mismatch" not in row["match_reasons"]


def test_parent_live_close_word_does_not_create_a_deadline_or_fabricated_quote() -> None:
    result = parse_job_page(
        _role_page(
            "2027 Placement Program - EMEA",
            PARENT_LIVE_BLACKROCK_DESCRIPTION_HTML,
        ),
        PARENT_LIVE_BLACKROCK_URL,
        expected_title="2027 Placement Program - EMEA",
        expected_employer="BlackRock",
        observed_at="2026-09-11T12:00:00+00:00",
    )

    assert result.availability == "open"
    assert result.deadline is None
    assert result.deadline_text == ""
    assert not any(
        item["quote"] == PARENT_LIVE_BLACKROCK_DEADLINE_ARTIFACT
        for item in result.evidence
    )


def test_parent_live_html_description_evidence_is_clean_and_provenanced() -> None:
    title = "2027 Placement Program - EMEA"
    result = parse_job_page(
        _role_page(
            title,
            PARENT_LIVE_BLACKROCK_DESCRIPTION_HTML,
            jsonld={
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": title,
                "description": PARENT_LIVE_BLACKROCK_DESCRIPTION_HTML,
                "hiringOrganization": {"name": "BlackRock"},
            },
        ),
        PARENT_LIVE_BLACKROCK_URL,
        expected_title=title,
        expected_employer="BlackRock",
        observed_at="2026-09-11T12:00:00+00:00",
    )

    assert result.evidence
    assert all("<" not in item["quote"] and ">" not in item["quote"] for item in result.evidence)
    assert all(item["source_url"] == PARENT_LIVE_BLACKROCK_URL for item in result.evidence)
    assert all(item["observed_at"] == "2026-09-11T12:00:00+00:00" for item in result.evidence)


def test_parent_live_explicit_airbus_date_keeps_date_provenance() -> None:
    result = parse_job_page(
        _role_page(
            "AI Solutions Engineer Placement (12.5 months)",
            PARENT_LIVE_AIRBUS_DESCRIPTION_QUOTE,
        ),
        "https://ag.wd3.myworkdayjobs.com/en-US/Airbus/job/fixture",
        expected_title="AI Solutions Engineer Placement (12.5 months)",
        expected_employer="Airbus",
        observed_at="2026-09-11T12:00:00+00:00",
    )

    assert result.deadline == "2026-09-27"
    assert result.deadline_text == "27 September 2026"
    assert result.deadline_basis == "explicit_text"
    date_evidence = [item for item in result.evidence if item["quote"] == "27 September 2026"]
    assert date_evidence == [
        {
            "quote": "27 September 2026",
            "source_url": "https://ag.wd3.myworkdayjobs.com/en-US/Airbus/job/fixture",
            "observed_at": "2026-09-11T12:00:00+00:00",
        }
    ]


def test_missing_deadline_is_not_inferred_from_an_unlabelled_start_date() -> None:
    result = parse_job_page(
        _role_page(
            "Industrial Placement",
            "Start date: 16 August 2027. Duration: 12.5 months.",
        ),
        "https://jobs.example.test/roles/industrial-placement",
        expected_title="Industrial Placement",
        expected_employer="Example Bank",
    )

    assert result.availability == "open"
    assert result.deadline is None
    assert result.deadline_text == ""
    assert not any(item["quote"].startswith("Deadline:") for item in result.evidence)


def test_source_listing_deadline_retains_source_listing_provenance_when_page_has_none(
    tmp_path: Path,
) -> None:
    job = _job(deadline="2026-10-15", deadline_text="15 Oct")
    row = _run_match(tmp_path, job, "Finance degree. Sponsorship available. 2:1 required.")

    assert row["deadline"] == "2026-10-15"
    assert row["deadline_text"] == "15 Oct"
    assert row["deadline_basis"] == "source_listing"


@pytest.mark.parametrize(
    ("criteria", "should_exclude"),
    [
        (
            "Finance degree. Master's degree required. Candidates graduating in 2028. "
            "Sponsorship available. 2:1 required.",
            True,
        ),
        (
            "Finance degree. Master's degree preferred, but a Bachelor's degree is "
            "accepted. Candidates graduating in 2028. Sponsorship available. 2:1 required.",
            False,
        ),
    ],
    ids=["mandatory-masters", "optional-masters"],
)
def test_masters_language_distinguishes_mandatory_from_optional(
    tmp_path: Path,
    criteria: str,
    should_exclude: bool,
) -> None:
    row = _run_match(tmp_path, _job(), criteria)

    assert (row["match_status"] == "excluded") is should_exclude, row
    if not should_exclude:
        assert "requires_masters" not in row["match_reasons"]


def test_degree_or_alternative_does_not_exclude_a_matching_finance_profile(
    tmp_path: Path,
) -> None:
    row = _run_match(
        tmp_path,
        _job(title="Summer Technology Internship 2028"),
        "Computer Science or Finance degree required. Candidates graduating in 2028. "
        "Sponsorship available. 2:1 required.",
    )

    assert row["match_status"] != "excluded", row
    assert "requires_stem_degree" not in row["match_reasons"]


@pytest.mark.parametrize(
    "criteria",
    [
        "Finance degree required. Candidates graduating in 2028 or 2029. "
        "Sponsorship available. 2:1 required.",
        "Finance degree required. Candidates in the class of 2028-2029 are eligible. "
        "Sponsorship available. 2:1 required.",
    ],
    ids=["graduation-alternative", "graduation-range"],
)
def test_year_alternatives_and_ranges_accept_a_target_year(
    tmp_path: Path,
    criteria: str,
) -> None:
    row = _run_match(tmp_path, _job(), criteria)

    assert row["match_status"] != "excluded", row
    assert "graduation_year_mismatch" not in row["match_reasons"]


def test_missing_graduation_evidence_is_review_not_a_match(tmp_path: Path) -> None:
    row = _run_match(
        tmp_path,
        _job(),
        "Finance degree. Sponsorship available. 2:1 required.",
    )

    assert row["match_status"] == "review", row
    assert any("graduation" in item for item in row["match_unknowns"])


def test_sponsorship_and_grade_presence_do_not_claim_candidate_satisfaction(
    tmp_path: Path,
) -> None:
    row = _run_match(
        tmp_path,
        _job(),
        "Finance degree required. Candidates graduating in 2028. Sponsorship available. "
        "2:1 required.",
    )

    assert row["match_status"] == "review", row
    assert "sponsorship" in row["match_unknowns"]
    assert "grades" in row["match_unknowns"]


def test_near_match_unrelated_jsonld_cannot_supply_role_facts() -> None:
    expected_title = "Summer Finance Analyst Internship 2028"
    unrelated = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Summer Finance Analyst Internship 2027",
        "description": "UNRELATED JSON-LD posting; New York, United States.",
        "hiringOrganization": {"name": "Example Bank"},
        "jobLocation": {"address": {"addressLocality": "New York"}},
    }
    result = parse_job_page(
        _role_page(expected_title, "The expected London role page.", jsonld=unrelated),
        "https://jobs.example.test/roles/finance-2028",
        expected_title=expected_title,
        expected_employer="Example Bank",
    )

    assert result.availability == "open"
    assert result.title == expected_title
    assert result.location == ""
    assert "unrelated" not in result.role_text.casefold()


def test_cached_facts_are_not_reused_for_a_different_url_with_the_same_id(
    tmp_path: Path,
) -> None:
    original = _job(
        job_id=77,
        url="https://jobs.example.test/roles/original",
        title="Summer Finance Analyst Internship 2028",
    )
    service = EnrichmentService(
        tmp_path,
        profile=copy.deepcopy(TEST_PROFILE),
        fetcher=_facts_fetcher(
            original,
            "Finance degree. Candidates graduating in 2028. Sponsorship available. 2:1 required.",
        ),
    )
    assert service.run([original])["checked"] == 1

    foreign = _job(
        job_id=77,
        url="https://jobs.example.test/roles/foreign",
        title="Summer Risk Internship 2028",
    )
    row = service.decorate([foreign])[0]

    assert row["availability"] == "unknown"
    assert row["verified_at"] is None
    assert row["evidence"] == []


def test_due_queue_count_reports_all_remaining_due_jobs_not_the_batch_cap(
    tmp_path: Path,
) -> None:
    jobs = [
        _job(job_id=index, url=f"https://jobs.example.test/roles/{index}")
        for index in range(1, 6)
    ]
    service = EnrichmentService(
        tmp_path,
        profile=copy.deepcopy(TEST_PROFILE),
        max_batch=2,
        fetcher=lambda _url, **_kwargs: {
            "availability": "unknown",
            "verification_error": "fixture verification incomplete",
        },
    )

    outcome = service.run(jobs)

    assert outcome["checked"] == 2
    assert outcome["queued"] == 3
    assert outcome["queued"] == service.summary()["queued"]


def _assert_unexpected_fetcher_shape_is_closed(tmp_path: Path, raw: object) -> None:
    job = _job()
    service = EnrichmentService(
        tmp_path,
        profile=copy.deepcopy(TEST_PROFILE),
        fetcher=lambda _url, **_kwargs: raw,
    )

    service.run([job])
    row = service.decorate([job])[0]

    assert row["availability"] == "unknown", row
    assert row["match_status"] != "potential"


@pytest.mark.parametrize(
    "raw",
    [
        {"availability": "open"},
        {"availability": 1, "status_code": 200},
        {"status_code": "not-a-number", "body": "<html></html>"},
        [],
    ],
    ids=["open-without-role-facts", "numeric-availability", "bad-status-string", "list"],
)
def test_unexpected_fetcher_shape_cases(
    tmp_path: Path,
    raw: object,
) -> None:
    _assert_unexpected_fetcher_shape_is_closed(tmp_path, raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("degree", 2028),
        ("desired_roles", "summer"),
        ("desired_locations", ["UK", 1]),
    ],
    ids=["numeric-degree", "string-roles", "mixed-location-list"],
)
def test_profile_shape_mismatches_are_rejected(field: str, value: object) -> None:
    profile = copy.deepcopy(TEST_PROFILE)
    profile[field] = value

    with pytest.raises(ProfileValidationError):
        validate_profile(profile)


def test_redirect_to_private_target_is_rejected_before_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PublicWebClient()
    resolved = ResolvedURL(
        "https://public.example/role",
        "https",
        "public.example",
        443,
        ("93.184.216.34",),
    )
    requests: list[str] = []

    monkeypatch.setattr(public_web, "resolve_public_url", lambda _url: resolved)
    monkeypatch.setattr(
        client,
        "_request",
        lambda value: requests.append(value.url)
        or (302, {"location": "http://127.0.0.1/internal"}, b""),
    )

    with pytest.raises(PublicWebError, match="private|special"):
        client.fetch("https://public.example/role")
    assert requests == ["https://public.example/role"]


def test_public_redirect_is_resolved_again_and_final_url_is_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PublicWebClient()
    first = "https://public.example/role"
    second = "https://public.example/role-final"
    resolved_urls: list[str] = []
    responses = iter(
        [
            (302, {"location": second}, b""),
            (
                200,
                {"content-type": "text/html"},
                _role_page("Summer Finance Analyst Internship 2028", "London role").encode(),
            ),
        ]
    )

    def resolve(url: str) -> ResolvedURL:
        resolved_urls.append(url)
        return ResolvedURL(url, "https", "public.example", 443, ("93.184.216.34",))

    monkeypatch.setattr(public_web, "resolve_public_url", resolve)
    monkeypatch.setattr(client, "_request", lambda _resolved: next(responses))

    result = client.fetch(
        first,
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
    )

    assert resolved_urls == [first, second]
    assert result.final_url == second
    assert result.availability == "open"




def test_dns_resolution_rejects_a_mixed_public_and_private_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def getaddrinfo(*_args: object, **_kwargs: object):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    with pytest.raises(PublicWebError, match="private|special"):
        public_web.resolve_public_url("https://public.example/role")


def test_pinned_http_connection_connects_to_the_validated_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object]] = []

    class SocketFixture:
        def close(self) -> None:
            pass

    def create_connection(address: object, timeout: object) -> SocketFixture:
        calls.append((address, timeout))
        return SocketFixture()

    monkeypatch.setattr(socket, "create_connection", create_connection)
    connection = public_web._PinnedHTTPConnection(
        "public.example",
        443,
        "93.184.216.34",
        timeout=2.5,
    )
    connection.connect()
    connection.close()

    assert calls == [(('93.184.216.34', 443), 2.5)]


def test_response_content_length_bound_is_enforced_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResponseFixture:
        status = 200

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Content-Length", "11")]

        def read(self, _size: int) -> bytes:
            raise AssertionError("oversized response should be rejected before reading")

    class ConnectionFixture:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> ResponseFixture:
            return ResponseFixture()

        def close(self) -> None:
            pass

    monkeypatch.setattr(public_web, "_PinnedHTTPConnection", ConnectionFixture)
    client = PublicWebClient(max_body_bytes=10)
    resolved = ResolvedURL(
        "http://public.example/role",
        "http",
        "public.example",
        80,
        ("93.184.216.34",),
    )

    with pytest.raises(PublicWebError):
        client._request(resolved)


def test_footer_captcha_is_not_a_role_challenge_but_in_scope_challenge_is_unknown() -> None:
    footer_only = (
        '<html><body><main><h1>Summer Finance Analyst Internship 2028</h1>'
        '<p>London role.</p><a href="/apply">Apply</a></main>'
        '<footer>Captcha provider notice and privacy settings.</footer></body></html>'
    )
    real_challenge = (
        '<html><body><main><h1>Summer Finance Analyst Internship 2028</h1>'
        '<p>Cloudflare CAPTCHA challenge: verify you are human.</p>'
        '<a href="/apply">Apply</a></main></body></html>'
    )

    footer_result = parse_job_page(
        footer_only,
        "https://jobs.example.test/roles/footer",
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
    )
    challenge_result = parse_job_page(
        real_challenge,
        "https://jobs.example.test/roles/challenge",
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
    )

    assert footer_result.availability == "open"
    assert challenge_result.availability == "unknown"
    assert "blocked" in challenge_result.verification_error


def test_generic_career_landing_page_is_unknown_not_closed() -> None:
    result = parse_job_page(
        (
            "<html><body><main><h1>Careers</h1><p>Explore internships and jobs.</p>"
            '<a href="/apply">Apply</a></main></body></html>'
        ),
        "https://example.test/careers",
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
    )

    assert result.availability == "unknown"
    assert result.availability != "closed"
    assert "generic" in result.verification_error


def test_role_without_application_control_remains_unknown_not_closed() -> None:
    result = parse_job_page(
        _role_page("Summer Finance Analyst Internship 2028", "London role.", apply=False),
        "https://jobs.example.test/roles/no-apply",
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
    )

    assert result.availability == "unknown"
    assert result.availability != "closed"
    assert "apply" in result.verification_error
