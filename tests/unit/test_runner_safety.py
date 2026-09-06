"""Regression tests for runner destination and network trust boundaries."""
from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.automation.runner as runner
from app.automation.runner import _trusted_network_request_allowed
from app.config import Settings
from app.models import Opportunity


@dataclass
class _FakePage:
    url: str
    identity: dict[str, str]

    def evaluate(self, _script: str) -> dict[str, str]:
        return self.identity


def _opportunity(*, employer: str, role_title: str, url: str) -> Opportunity:
    return Opportunity(
        employer=employer,
        role_title=role_title,
        cycle="2027",
        url=url,
    )


def test_destination_identity_rejects_partial_employer_and_role_matches() -> None:
    page = _FakePage(
        url="https://jobs.example.com/apply/other-role",
        identity={
            "employer": "",
            "role": "",
            "visible": "Acme Summer role — unrelated details",
        },
    )
    opportunity = _opportunity(
        employer="Acme Capital",
        role_title="Summer Analyst",
        url=page.url,
    )

    findings = runner._destination_findings(page, opportunity)

    assert {finding.code for finding in findings} == {
        "destination_employer_unverified",
        "destination_role_unverified",
    }


def test_destination_identity_accepts_case_and_punctuation_normalisation() -> None:
    page = _FakePage(
        url="https://jobs.example.com/apply/acme-capital-summer-analyst",
        identity={
            "employer": "ACME, CAPITAL",
            "role": "summer-analyst",
            "visible": "ACME, CAPITAL — SUMMER-ANALYST",
        },
    )
    opportunity = _opportunity(
        employer="Acme Capital",
        role_title="Summer Analyst",
        url=page.url,
    )

    assert runner._destination_findings(page, opportunity) == []


def test_destination_identity_accepts_initialism_punctuation() -> None:
    page = _FakePage(
        url="https://jobs.example.com/apply/jp-morgan-summer-analyst",
        identity={
            "employer": "J.P. MORGAN",
            "role": "SUMMER ANALYST",
            "visible": "JP Morgan — Summer Analyst",
        },
    )
    opportunity = _opportunity(
        employer="JP Morgan",
        role_title="Summer Analyst",
        url=page.url,
    )

    assert runner._destination_findings(page, opportunity) == []


def test_trusted_network_policy_blocks_unallowlisted_asset_gets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "jobs.example.com,cdn.example.net",
        }
    )
    policy = _trusted_network_request_allowed

    assert policy(
        settings,
        "https://jobs.example.com/assets/app.js",
        "https://jobs.example.com/apply",
    ) is True
    assert policy(
        settings,
        "https://cdn.example.net/fonts/app.woff2",
        "https://jobs.example.com/apply",
    ) is True

    for resource in ("pixel", "app.js", "app.css", "app.woff2", "beacon"):
        assert policy(
            settings,
            f"https://collector.example.net/{resource}?email=private@example.test",
            "https://jobs.example.com/apply",
        ) is False
