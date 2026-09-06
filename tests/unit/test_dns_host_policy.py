"""Deterministic DNS safety regressions for browser destinations."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

import app.automation.host_policy as host_policy
from app.automation.runner import (
    SubmissionBlocked,
    _network_destination_allowed,
    assert_field_fill_allowed,
    assert_submission_allowed,
)
from app.config import Settings


def _answers(*addresses: str):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
        for address in addresses
    ]


def test_public_hostname_requires_all_global_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _answers("93.184.216.34"),
    )

    assert host_policy.safe_public_navigation_url("https://jobs.example.com/apply") is True


def test_private_dns_answer_blocks_navigation_network_and_allowlisted_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _answers("192.168.1.20", "93.184.216.34"),
    )
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "jobs.example.com",
        }
    )

    assert host_policy.safe_public_navigation_url("https://jobs.example.com/apply") is False
    with pytest.raises(SubmissionBlocked):
        assert_field_fill_allowed(settings, "https://jobs.example.com/apply")
    with pytest.raises(SubmissionBlocked):
        assert_submission_allowed(settings, "https://jobs.example.com/apply", 0)
    assert _network_destination_allowed(settings, "https://jobs.example.com/api") is False


def test_unresolved_hostname_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror("resolver unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", fail_resolution)

    assert host_policy.safe_public_navigation_url("https://jobs.example.com/apply") is False
    assert host_policy.safe_public_network_url("https://jobs.example.com/app.js") is False


def test_literal_global_ip_does_not_require_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DNS should not run for IP literals")),
    )

    assert host_policy.safe_public_navigation_url("https://93.184.216.34/apply") is True
    assert host_policy.safe_public_network_url("https://93.184.216.34/app.js") is True
