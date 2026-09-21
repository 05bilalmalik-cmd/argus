from __future__ import annotations

import hashlib
import json
import socket

import pytest

from app.tracker.public_web import (
    PublicWebError,
    WebPageResult,
    parse_job_page,
    validate_public_url,
)


def _page(*, title: str = "Summer Finance Analyst Internship 2028", apply: bool = True) -> str:
    apply_markup = '<a class="apply-button" href="/apply">Apply now</a>' if apply else ""
    first = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Unrelated Navigation Job", "description": "nav item"}
    second = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": "Finance or Economics degree. Sponsorship available. 2:1 required.",
        "hiringOrganization": {"name": "Example Bank"},
        "datePosted": "2026-09-01",
        "validThrough": "2027-01-15",
        "url": "https://jobs.example.test/roles/finance-2028",
    }
    return f"""
    <html><head>
      <script type="application/ld+json">{json.dumps(first)}</script>
      <script type="application/ld+json">{json.dumps(second)}</script>
    </head><body>
      <nav>Summer Finance Analyst Internship 2028 unrelated navigation</nav>
      <main><h1>{title}</h1><p>Applications close 15 January 2027.</p>{apply_markup}</main>
    </body></html>
    """


def test_jsonld_selects_matching_job_not_first_graph_and_keeps_role_evidence() -> None:
    result = parse_job_page(
        _page(),
        "https://jobs.example.test/roles/finance-2028",
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
        observed_at="2026-09-11T12:00:00+00:00",
    )

    assert result.availability == "open"
    assert result.deadline == "2027-01-15"
    assert result.deadline_text == "15 January 2027"
    assert result.deadline_basis == "explicit_valid_through"
    assert result.posted_at == "2026-09-01T00:00:00+00:00"
    assert result.posted_text == "2026-09-01"
    assert any("Finance or Economics" in item["quote"] for item in result.evidence)
    assert all(set(item) == {"quote", "source_url", "observed_at"} for item in result.evidence)
    assert len(result.source_response_hash or "") == 64


def test_absent_apply_button_and_generic_navigation_do_not_become_open() -> None:
    result = parse_job_page(
        _page(apply=False),
        "https://jobs.example.test/careers",
        expected_title="Summer Finance Analyst Internship 2028",
        expected_employer="Example Bank",
    )

    assert result.availability == "unknown"
    assert "apply" in result.verification_error.lower()
    assert result.evidence


def test_waf_shell_and_redirected_careers_page_are_unknown() -> None:
    waf = parse_job_page(
        "<html><body><h1>Access denied</h1><p>Verify you are human</p></body></html>",
        "https://jobs.example.test/roles/finance-2028",
        expected_title="Summer Finance Analyst Internship 2028",
    )
    redirected = parse_job_page(
        "<html><body><nav>Careers Jobs Opportunities</nav><h1>Explore careers</h1></body></html>",
        "https://example.test/careers",
        expected_title="Summer Finance Analyst Internship 2028",
        final_url="https://example.test/careers",
    )

    assert waf.availability == "unknown"
    assert redirected.availability == "unknown"
    assert "blocked" in waf.verification_error.lower() or "shell" in waf.verification_error.lower()
    assert "generic" in redirected.verification_error.lower()


def test_missing_dates_preserves_yearless_text_and_does_not_invent_year() -> None:
    html = """
    <html><body><main><h1>Spring Insight Week</h1>
      <p>Deadline: 15 October</p><a href="/apply">Apply</a>
    </main></body></html>
    """
    result = parse_job_page(
        html,
        "https://jobs.example.test/roles/spring-insight",
        expected_title="Spring Insight Week",
        expected_employer="Example Bank",
    )

    assert result.availability == "open"
    assert result.deadline is None
    assert result.deadline_text == "15 October"
    assert result.deadline_basis == "unknown_year"


def test_wrong_year_is_retained_for_matcher_and_not_normalised_away() -> None:
    result = parse_job_page(
        _page(title="Summer Finance Analyst Internship 2027"),
        "https://jobs.example.test/roles/finance-2027",
        expected_title="Summer Finance Analyst Internship 2027",
    )

    assert result.deadline == "2027-01-15"
    assert result.posted_at == "2026-09-01T00:00:00+00:00"
    assert "2027" in result.deadline_text


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "http://127.0.0.1/job",
        "http://[::1]/job",
        "https://user:pass@example.test/job",
        "ftp://example.test/job",
        "https://localhost/job",
    ],
)
def test_public_url_validation_rejects_ssrf_and_credentials(url: str) -> None:
    with pytest.raises(PublicWebError):
        validate_public_url(url)


def test_web_result_has_hash_only_not_html_payload() -> None:
    result = WebPageResult(
        availability="unknown",
        verification_error="blocked",
        body=b"private page body",
        source_url="https://jobs.example.test/role",
    )

    assert result.source_response_hash == hashlib.sha256(b"private page body").hexdigest()
    assert not hasattr(result, "html")
    assert result.as_dict()["source_response_hash"] == result.source_response_hash


def test_dns_rebinding_answer_with_private_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*args: object, **kwargs: object):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    from app.tracker.public_web import resolve_public_url

    with pytest.raises(PublicWebError, match="private|special"):
        resolve_public_url("https://public.example/role")
