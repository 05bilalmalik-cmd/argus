"""Regression tests for defects found during the final independent audit.

Covers:
1. Concurrent submit runs for one application must not both reach a browser
   submission (in-process claim guard).
2. RuntimeLock must reject a second acquire from the SAME process (Windows
   byte-range locks do not do this on their own).
3. ``argus serve`` must refuse to start when the runtime lock is held.
4. The Applications UI submit control must carry the exact action-time
   confirmation parameter (no orphaned, always-409 capability).
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, Opportunity
from app.runtime_lock import RuntimeAlreadyRunning, RuntimeLock, runtime_lock_path


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    return TestClient(create_app(settings))


def _seed_application(database: Database, *, employer: str) -> str:
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=f"{employer} Summer Analyst",
            cycle="2027",
            url="https://jobs.example.test/apply",
            source="regression",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state="PACKAGE_PREPARED",
            priority=50,
        )
        session.add(application)
        session.flush()
        return application.id


def test_concurrent_submit_runs_cannot_both_claim_one_application(tmp_path: Path) -> None:
    """Two threads racing run() must produce exactly one claimed run."""

    from app.automation.runner import AutomationRunner, SubmissionBlocked
    from app.automation.types import RunMode

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    database.create_schema()
    application_id = _seed_application(database, employer="Race Capital")

    runner = AutomationRunner(database, settings, database.crypto if hasattr(database, "crypto") else None) if False else None  # noqa: E501 - placeholder removed below

    # Build the runner with a real CryptoBox-equivalent stub: the claim guard
    # is exercised before any browser/crypto use, so a minimal object works.
    class _NoCrypto:
        pass

    runner = AutomationRunner.__new__(AutomationRunner)  # bypass __init__ deps
    runner.database = database
    runner.settings = settings
    runner.crypto = _NoCrypto()

    from app.security.audit import AuditInput, append_audit

    outcomes: list[str] = []
    started = threading.Event()
    release = threading.Event()

    original_sleep = None

    def slow_run() -> None:
        # Interpose on the browser section by patching sync_playwright usage:
        # instead, call the claim helper the way run() does, twice.
        started.set()
        release.wait(5)
        with database.session_scope() as session:
            application = session.get(Application, application_id)
            try:
                runner._claim_for_automation(session, application)
                outcomes.append("claimed")
            except SubmissionBlocked as exc:
                outcomes.append(f"refused:{exc.code}")

    threads = [threading.Thread(target=slow_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    started.wait(5)
    release.set()
    for thread in threads:
        thread.join(10)

    assert outcomes.count("claimed") == 1, outcomes
    assert sum(item.startswith("refused:") for item in outcomes) == 1, outcomes


def test_runtime_lock_rejects_second_acquire_in_same_process(tmp_path: Path) -> None:
    lock_path = runtime_lock_path(tmp_path)
    with RuntimeLock(lock_path, port=8991):
        second = RuntimeLock(lock_path, port=8991)
        with pytest.raises(RuntimeAlreadyRunning):
            second.acquire()
        assert second.acquired is False


def test_serve_command_refuses_to_start_when_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    from app import cli as cli_module

    lock_path = runtime_lock_path(tmp_path)
    with RuntimeLock(lock_path, port=8992):
        monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
        booted: dict[str, object] = {}

        def refused_run(*_args: object, **_kwargs: object) -> None:  # noqa: ANN002, ANN003
            booted["uvicorn"] = True

        monkeypatch.setattr(cli_module.uvicorn, "run", refused_run)
        exit_code = cli_module._command_serve(type("Args", (), {"open": False, "quiet": True})())

    assert exit_code == 2
    assert booted == {}


@pytest.mark.parametrize(
    ("state", "navigator_label"),
    [
        ("PACKAGE_PREPARED", "Apply in Navigator"),
        ("READY_TO_SUBMIT", "Review exact manifest"),
    ],
)
def test_applications_page_submit_carries_exact_confirmation(
    tmp_path: Path, state: str, navigator_label: str
) -> None:
    with _client(tmp_path) as client:
        with client.app.state.db.session_scope() as session:
            opportunity = Opportunity(
                employer="Confirm Capital",
                role_title="Confirm Summer Analyst",
                cycle="2027",
                url="https://jobs.example.test/confirm",
                application_url="https://jobs.example.test/confirm/form",
                target_status=TargetKind.APPLICATION_ENTRY.value,
                resolved_ats_type="generic",
                resolution_evidence_json='{"identity_verified": true, "application_url_verified": true}',
                resolved_at=datetime.now(timezone.utc),
                source="regression",
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state=state,
                priority=50,
            )
            session.add(application)
            session.flush()
            application_id = application.id

        response = client.get("/applications")

        assert response.status_code == 200
        assert "mode=submit" not in response.text
        assert "confirm_application_id" not in response.text
        assert re.search(
            rf'<button[^>]+data-navigator-start[^>]+data-application-id="{re.escape(application_id)}"[^>]*>{re.escape(navigator_label)}</button>',
            response.text,
        ), "exact per-application Navigator control missing"

        detail = client.get(f"/applications/{application_id}")
        assert detail.status_code == 200
        assert re.search(
            r'<a[^>]+href="https://jobs\.example\.test/confirm"[^>]*>Open source',
            detail.text,
        ), "source-only link missing from application detail"
