"""Global fail-closed test containment for ARGUS.

The application is exercised against loopback fixtures only.  This fixture
removes inherited safety-sensitive ARGUS/proxy settings and rejects accidental
real-network connections from the pytest process while preserving local HTTP,
SQLite, and Playwright loopback traffic.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import pytest

from tests.source_stability import (
    diff_source_snapshots,
    format_source_drift,
    snapshot_source_tree,
)


_SAFETY_ENV = (
    "ARGUS_UI_V2",
    "ARGUS_AUTOMATION_MODE",
    "ARGUS_AUTOMATION_STATE",
    "ARGUS_AUTOPILOT_MODE",
    "ARGUS_AUTOMATION",
    "ARGUS_ENABLE_LIVE_SUBMIT",
    "ARGUS_ENABLE_TRACKR_LIVE",
    "ARGUS_ENABLE_APPLY_CLICK",
    "ARGUS_ENABLE_EGRESS_IMPACT_CLASSIFICATION",
    "ARGUS_APPLY_CLICK_RUN_CAP",
    "ARGUS_APPLY_CLICK_TIMEOUT_SECONDS",
    "ARGUS_LIVE_DOMAIN_ALLOWLIST",
    "ARGUS_AUTOPILOT_SUBMIT",
    "ARGUS_CHROMIUM_EXECUTABLE",
    "ARGUS_API_TOKEN",
    "ARGUS_SIGNING_PASSWORD",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def pytest_sessionstart(session: pytest.Session) -> None:
    """Freeze all Python source before collection can observe mixed revisions."""

    session.config._argus_source_baseline = snapshot_source_tree(_PROJECT_ROOT)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if another process rewrote source during collection/tests."""

    baseline = getattr(session.config, "_argus_source_baseline", None)
    if not isinstance(baseline, dict):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    try:
        current = snapshot_source_tree(_PROJECT_ROOT)
        changes = diff_source_snapshots(baseline, current)
        description = format_source_drift(changes)
    except (OSError, RuntimeError) as exc:
        description = f"source stability verification failed: {type(exc).__name__}"
    if not description:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep(
            "=",
            "ARGUS source changed during pytest; results are invalid",
            red=True,
        )
        reporter.write_line(description, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _loopback_host(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        # AF_UNIX/named-pipe addresses are not internet destinations.
        return True
    host = str(address[0]).strip().casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def contain_test_runtime(monkeypatch: pytest.MonkeyPatch):
    for name in _SAFETY_ENV:
        monkeypatch.delenv(name, raising=False)

    original_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: object):
        if not _loopback_host(address):
            raise AssertionError(
                f"test attempted non-loopback network connection to {address!r}"
            )
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
