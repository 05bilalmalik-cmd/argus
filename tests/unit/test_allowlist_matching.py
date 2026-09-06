"""Tests for the improvement pass: allowlist suffix matching and fuzzy
answer-bank resolution."""
from __future__ import annotations

import socket

import pytest

from app.automation.runner import (
    SubmissionBlocked,
    _network_destination_allowed,
    assert_field_fill_allowed,
    assert_submission_allowed,
)
from app.automation.host_policy import safe_public_network_url
from app.config import Settings


@pytest.fixture()
def settings() -> Settings:
    s = Settings.load(
        {
            "ARGUS_DATA_DIR": r"C:/Users/demo/AppData/Local/Temp/argus_test_fixtures",
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": (
                "boards.greenhouse.io,job-boards.greenhouse.io,jobs.lever.co,"
                "*.myworkdayjobs.com,*.myworkdaysite.com,*.taleo.net,*.smartrecruiters.com"
            ),
        }
    )
    return s


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )


def test_suffix_allowlist_matches_workday_subdomains(settings: Settings) -> None:
    # Subdomain access is explicit in the wildcard entry.
    assert_field_fill_allowed(
        settings, "https://carlyle.wd103.myworkdayjobs.com/en-US/job/123"
    )
    assert_field_fill_allowed(
        settings, "https://boards.greenhouse.io/firm/jobs/1"
    )


def test_unrelated_domain_still_blocked(settings: Settings) -> None:
    with pytest.raises(SubmissionBlocked):
        assert_field_fill_allowed(settings, "https://evil.example.com/form")
    # a domain that merely CONTAINS an entry but isn't a subdomain must fail
    with pytest.raises(SubmissionBlocked):
        assert_field_fill_allowed(
            settings, "https://myworkdayjobs.com.evil.example.com/form"
        )


def test_submission_suffix_match(settings: Settings) -> None:
    assert_submission_allowed(
        settings, "https://firm.wd5.myworkdayjobs.com/submit", risk_level=0
    )
    with pytest.raises(SubmissionBlocked):
        assert_submission_allowed(
            settings, "https://not-allowlisted.example.com/submit", risk_level=0
        )


def test_subdomain_access_requires_explicit_wildcard(settings: Settings) -> None:
    exact = Settings.load(
        {
            "ARGUS_DATA_DIR": str(settings.data_dir),
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "myworkdayjobs.com",
        }
    )
    with pytest.raises(SubmissionBlocked):
        assert_field_fill_allowed(
            exact, "https://firm.wd5.myworkdayjobs.com/123"
        )

    wildcard = Settings.load(
        {
            "ARGUS_DATA_DIR": str(settings.data_dir),
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "*.myworkdayjobs.com",
        }
    )
    assert_field_fill_allowed(wildcard, "https://firm.wd5.myworkdayjobs.com/123")
    with pytest.raises(SubmissionBlocked):
        assert_field_fill_allowed(wildcard, "https://myworkdayjobs.com/123")


def test_navigation_and_network_checks_share_exact_host_policy(settings: Settings) -> None:
    assert _network_destination_allowed(
        settings, "https://firm.wd5.myworkdayjobs.com/api"
    ) is True
    assert _network_destination_allowed(
        settings, "https://myworkdayjobs.com.evil.example/api"
    ) is False
    assert _network_destination_allowed(
        settings, "https://evil.example.com/api"
    ) is False


def test_review_request_guard_rejects_private_hops_but_allows_public_assets() -> None:
    assert safe_public_network_url("http://cdn.example.com/app.js") is True
    assert safe_public_network_url("wss://jobs.example.com/socket") is True
    assert safe_public_network_url("http://127.0.0.1:8080/private") is False
    assert safe_public_network_url("http://169.254.169.254/latest/meta-data") is False


def test_exact_host_policy_does_not_trust_non_web_schemes(settings: Settings) -> None:
    with pytest.raises(SubmissionBlocked):
        assert_field_fill_allowed(settings, "file://boards.greenhouse.io/form")
    with pytest.raises(SubmissionBlocked):
        assert_submission_allowed(settings, "file://boards.greenhouse.io/form", 0)
    assert _network_destination_allowed(settings, "file://boards.greenhouse.io/form") is False
