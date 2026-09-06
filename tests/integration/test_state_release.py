from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, AuditEvent, AutomationRun, Opportunity
from app.runtime_lock import RuntimeAlreadyRunning, RuntimeLock
from app.security.audit import _hash_event
from requeue_blocked import plan_repair, repair_database
from scripts.package_release import _issue_release_capability, build_release, source_snapshot
from scripts.verify import _command_hash, _plan_digest, _step_plan
import launcher


def _database(tmp_path: Path) -> Database:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    database.create_schema()
    return database


def _old_requeue_event(session, *, application_id: str, event_id: int, created_at: str) -> None:
    details_json = json.dumps(
        {"reason": "allowlist and destination checks improved"},
        separators=(",", ":"),
        sort_keys=True,
    )
    previous = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    previous_hash = previous.event_hash if previous else ""
    event = AuditEvent(
        id=event_id,
        created_at=created_at,
        actor="maintenance",
        event_type="application.requeued",
        entity_type="application",
        entity_id=application_id,
        details_json=details_json,
        previous_hash=previous_hash,
        event_hash=_hash_event(
            previous_hash,
            created_at,
            "maintenance",
            "application.requeued",
            "application",
            application_id,
            details_json,
        ),
    )
    session.add(event)
    session.flush()


def test_unassessed_application_risk_is_not_reported_as_zero(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Test Employer",
            role_title="Intern",
            cycle="2027",
            url="https://example.test/role",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
        )
        session.add(application)
        session.flush()

        assert application.risk_level == 0  # legacy compatibility
        assert application.risk_is_assessed is False
        assert application.effective_risk_level is None

        application.mark_risk_assessed(0, source="unit-test")
        assert application.risk_is_assessed is True
        assert application.effective_risk_level == 0


def test_create_schema_adds_nullable_risk_metadata_to_old_database(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE applications (
          id VARCHAR(36) PRIMARY KEY,
          opportunity_id VARCHAR(36) NOT NULL,
          state VARCHAR(60) NOT NULL,
          priority INTEGER NOT NULL,
          risk_level INTEGER NOT NULL,
          selected_cv_id VARCHAR(36),
          selected_cover_letter_id VARCHAR(36),
          eligibility_json TEXT NOT NULL,
          conflict_json TEXT NOT NULL,
          next_action VARCHAR(240) NOT NULL,
          next_action_deadline DATETIME,
          submission_reference VARCHAR(240) NOT NULL,
          applied_at DATETIME,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    database = _database(tmp_path)
    with database.engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(applications)"))
        }
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
    assert {"risk_assessed_at", "risk_assessment_source"} <= columns
    assert version >= 1


def test_schema_backfills_risk_only_from_finished_terminal_automation_runs(tmp_path: Path) -> None:
    database = _database(tmp_path)
    finished_at = datetime(2026, 8, 22, 23, 40, tzinfo=timezone.utc)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Evidence Employer",
            role_title="Intern",
            cycle="2027",
            url="https://example.test/evidence",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.NEEDS_USER.value,
            risk_level=3,
        )
        session.add(application)
        session.flush()
        run = AutomationRun(
            application_id=application.id,
            mode="review",
            state=ApplicationState.NEEDS_USER.value,
            risk_level=3,
            finished_at=finished_at,
        )
        unfinished = AutomationRun(
            application_id=application.id,
            mode="review",
            state=ApplicationState.FILLING.value,
            risk_level=4,
            finished_at=None,
        )
        session.add_all([run, unfinished])

    # A second schema call represents an upgrade of an already-existing DB;
    # the migration must be idempotent and use only terminal finished runs.
    database.create_schema()
    with database.session_scope() as session:
        refreshed_application = session.get(Application, application.id)
        refreshed_run = session.get(AutomationRun, run.id)
        refreshed_unfinished = session.get(AutomationRun, unfinished.id)
        assert refreshed_run.risk_is_assessed is True
        assert refreshed_run.risk_assessed_at.replace(tzinfo=timezone.utc) == finished_at
        assert refreshed_run.risk_assessment_source == f"legacy_automation_run:{run.id}"
        assert refreshed_application.risk_is_assessed is True
        assert refreshed_application.risk_level == 3
        assert refreshed_application.risk_assessment_source == f"legacy_automation_run:{run.id}"
        assert refreshed_unfinished.risk_is_assessed is False


def test_runtime_lock_rejects_second_argus_instance_and_releases_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "argus.runtime.lock"
    first = RuntimeLock(path, host="127.0.0.1", port=8787, version="test")
    second = RuntimeLock(path, host="127.0.0.1", port=8787, version="test")

    first.acquire()
    try:
        with pytest.raises(RuntimeAlreadyRunning):
            second.acquire()
    finally:
        first.release()

    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["port"] == 8787
    assert metadata["version"] == "test"

    second.acquire()
    second.release()


def test_requeue_plan_uses_the_audited_batch_and_preserves_advanced_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    timestamp = "2026-08-22T23:34:31.327121+00:00"
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Test Employer",
            role_title="Intern",
            cycle="2027",
            url="https://example.test/requeue",
        )
        session.add(opportunity)
        session.flush()
        first = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.ELIGIBILITY_CHECKED.value,
            risk_level=4,
        )
        second_opp = Opportunity(
            employer="Test Employer 2",
            role_title="Intern",
            cycle="2027",
            url="https://example.test/advanced",
        )
        session.add(second_opp)
        session.flush()
        advanced = Application(
            opportunity_id=second_opp.id,
            state=ApplicationState.NEEDS_USER.value,
            risk_level=4,
        )
        session.add_all([first, advanced])
        session.flush()
        _old_requeue_event(session, application_id=first.id, event_id=1, created_at=timestamp)
        _old_requeue_event(
            session,
            application_id=advanced.id,
            event_id=2,
            created_at="2026-08-22T23:34:31.328000+00:00",
        )

        report = plan_repair(session)
        assert report.candidate_count == 2
        assert report.to_block_count == 1
        assert report.skipped_count == 1
        assert report.candidates[0].application_id == first.id
        assert report.candidates[0].current_state == ApplicationState.ELIGIBILITY_CHECKED.value


def test_requeue_repair_is_dry_run_by_default_and_apply_is_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Test Employer",
            role_title="Intern",
            cycle="2027",
            url="https://example.test/apply",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.ELIGIBILITY_CHECKED.value,
            risk_level=4,
        )
        session.add(application)
        session.flush()
        _old_requeue_event(
            session,
            application_id=application.id,
            event_id=1,
            created_at="2026-08-22T23:34:31.327121+00:00",
        )

    dry_run = repair_database(settings, apply=False)
    assert dry_run.repaired_count == 0
    with database.session_scope() as session:
        assert session.get(Application, application.id).state == ApplicationState.ELIGIBILITY_CHECKED.value

    applied = repair_database(settings, apply=True)
    assert applied.repaired_count == 1
    assert applied.backup_path is not None and applied.backup_path.is_file()
    with database.session_scope() as session:
        repaired = session.get(Application, application.id)
        assert repaired.state == ApplicationState.BLOCKED.value
        assert repaired.risk_level == 4
        assert session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "application.requeue_repaired")
        ) is not None

    repeated = repair_database(settings, apply=True)
    assert repeated.repaired_count == 0


def test_repair_checks_audit_chain_before_migrating_source_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Tampered Employer",
            role_title="Intern",
            cycle="2027",
            url="https://example.test/tampered",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.NEEDS_USER.value,
            risk_level=3,
        )
        session.add(application)
        session.flush()
        run = AutomationRun(
            application_id=application.id,
            mode="review",
            state=ApplicationState.NEEDS_USER.value,
            risk_level=3,
            finished_at=datetime(2026, 8, 22, 23, 40),
        )
        session.add(run)
        session.flush()
        event = AuditEvent(
            created_at="2026-08-22T23:40:00+00:00",
            actor="test",
            event_type="fixture.created",
            entity_type="application",
            entity_id=application.id,
            details_json="{\"safe\":true}",
            previous_hash="",
            event_hash=_hash_event(
                "",
                "2026-08-22T23:40:00+00:00",
                "test",
                "fixture.created",
                "application",
                application.id,
                "{\"safe\":true}",
            ),
        )
        session.add(event)
        session.flush()
        event.details_json = "{\"safe\":false}"

    with pytest.raises(RuntimeError, match="audit chain"):
        repair_database(settings, apply=True)

    with database.session_scope() as session:
        assert session.get(Application, application.id).risk_is_assessed is False
        assert session.get(AutomationRun, run.id).risk_is_assessed is False


def test_release_manifest_is_deterministic_and_contains_test_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    output = root / "dist"
    for relative in (
        "app/main.py",
        "extension/manifest.json",
        "scripts/setup.sh",
        "pyproject.toml",
        "requirements.txt",
        "README.md",
        "SECURITY.md",
        "OPERATIONS.md",
        "VERIFICATION.md",
        "tests/e2e/test_adversarial_flows.py",
        "tests/e2e/test_application_flows.py",
        "tests/e2e/test_application_navigator_journeys.py",
        "tests/e2e/test_apply_ui.py",
        "tests/e2e/test_dashboard.py",
        "tests/e2e/test_lab_adapter_variants.py",
        "tests/e2e/test_submission_unknown.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8")
    project_root = Path(__file__).resolve().parents[2]
    (root / "packaging" / "privacy_scan_config.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "packaging" / "privacy_scan_config.json").write_text((project_root / "packaging" / "privacy_scan_config.json").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "scripts" / "privacy_scan.py").write_text((project_root / "scripts" / "privacy_scan.py").read_text(encoding="utf-8"), encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "ARGUS Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    evidence_root = root / "release-evidence"; evidence_root.mkdir()
    names = ["unit", "integration", "tests/e2e/test_adversarial_flows.py", "tests/e2e/test_application_flows.py", "tests/e2e/test_application_navigator_journeys.py", "tests/e2e/test_apply_ui.py", "tests/e2e/test_dashboard.py", "tests/e2e/test_lab_adapter_variants.py", "tests/e2e/test_submission_unknown.py", "compile", "cli_help", "cli_safe_smoke", "audit_fresh_temp", "migration_fresh_temp", "privacy_source"]
    logs, steps = [], []
    nonce = "state-fixture-provenance-20260825"
    interpreter = Path(sys.executable).resolve()
    plan = _step_plan(root, str(interpreter), None)
    plan_sha256 = _plan_digest(plan)
    command_hashes_by_name = {item["name"]: [_command_hash(command) for command in (item.get("commands") or [item.get("command")])] for item in plan}
    for index, name in enumerate(names, 1):
        hashes = command_hashes_by_name[name]
        command_lines = "".join(f"command_sha256={value}\n" for value in hashes)
        log = evidence_root / f"{index:02d}.log"; log.write_text(f"started_at=2026-08-25T00:00:00.000Z\nstep={name}\nprovenance_nonce={nonce}\nplan_sha256={plan_sha256}\ncommand_count={len(hashes)}\n{command_lines}finished_at=2026-08-25T00:00:01.000Z\nexit_code=0\n", encoding="utf-8")
        entry = {"path": log.relative_to(root).as_posix(), "sha256": hashlib.sha256(log.read_bytes()).hexdigest(), "size": log.stat().st_size}
        logs.append(entry); steps.append({"name": name, "status": "passed", "exit_code": 0, "log": entry, "provenance_nonce": nonce, "plan_sha256": plan_sha256, "command_hashes": hashes})
    started = datetime.now(timezone.utc)
    evidence = {"schema_version": 2, "status": "passed", "started_at": started.isoformat().replace("+00:00", "Z"), "finished_at": (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), "live_network": False, "python": {"path": str(interpreter), "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(), "version": sys.version.split()[0], "implementation": "cpython", "executable": str(interpreter), "real": True}, "source": source_snapshot(root), "steps": steps, "logs": logs, "provenance": {"schema_version": 1, "run_nonce": nonce, "plan_sha256": plan_sha256, "executed": True}}

    first = build_release(
        root=root,
        output_dir=output,
        version="0.1.0",
        test_evidence=evidence,
        verification_capability=_issue_release_capability(evidence, root=root),
    )
    first_manifest = first.manifest_file
    assert first_manifest is not None and first_manifest.is_file()
    payload = json.loads(first_manifest.read_text(encoding="utf-8"))
    assert payload["archive_sha256"] == first.sha256
    assert payload["archive_verified_twice"] is True
    assert payload["archive_reproducible"] is True
    assert payload["test_evidence"]["status"] == "passed"
    assert payload["files"] == sorted(payload["files"], key=lambda item: item["path"])

    second = build_release(
        root=root,
        output_dir=output,
        version="0.1.0",
        test_evidence=evidence,
        verification_capability=_issue_release_capability(evidence, root=root),
    )
    assert second.sha256 == first.sha256
    assert second.manifest_file is not None
    assert second.manifest_file.read_bytes() == first_manifest.read_bytes()


def test_release_signing_fails_closed_without_certificate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="certificate"):
        build_release(
            root=tmp_path,
            output_dir=tmp_path / "dist",
            version="0.1.0",
            signing_requested=True,
        )


def test_release_signing_fails_closed_without_private_key(tmp_path: Path) -> None:
    certificate = tmp_path / "release.crt"
    certificate.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="key"):
        build_release(
            root=tmp_path,
            output_dir=tmp_path / "dist",
            version="0.1.0",
            signing_requested=True,
            signing_certificate=certificate,
        )


def test_launcher_refuses_busy_preferred_port_instead_of_fallback(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_port_free", lambda port: False)
    with pytest.raises(SystemExit, match="refusing to select another"):
        launcher._pick_port()


def test_launcher_safe_defaults_do_not_enable_live_submit_or_trackr(tmp_path: Path) -> None:
    assert launcher.DEFAULT_ENV["ARGUS_AUTOMATION_MODE"] == "OFF"
    assert launcher.DEFAULT_ENV["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
    assert launcher.DEFAULT_ENV["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
    assert launcher.DEFAULT_ENV["ARGUS_BROWSER_HEADLESS"] == "true"
    assert launcher.DEFAULT_ENV["ARGUS_BROWSER_OPEN"] == "false"
    assert launcher.DEFAULT_ENV["ARGUS_SWEEP_INTERVAL_HOURS"] == "0"
    assert launcher.DEFAULT_ENV["ARGUS_LIVE_DOMAIN_ALLOWLIST"] == ""
    assert not any(
        "trackr" in key.casefold() and value.casefold() in {"true", "1", "yes", "on"}
        for key, value in launcher.DEFAULT_ENV.items()
    )
    settings = Settings.load({**launcher.DEFAULT_ENV, "ARGUS_DATA_DIR": str(tmp_path)})
    assert settings.automation_mode.value == "OFF"
    assert settings.live_submit_enabled is False
    assert settings.trackr_live_enabled is False
    assert settings.sweep_interval_hours == 0
    assert settings.browser_headless is True


def test_launcher_child_environment_overwrites_inherited_safety_controls_and_drops_private_inputs(
    tmp_path: Path,
) -> None:
    inherited = {
        "PATH": "C:/safe/bin",
        "ARGUS_HOST": "0.0.0.0",
        "ARGUS_AUTOMATION_MODE": "ARMED",
        "ARGUS_AUTOMATION_STATE": "RUNNING",
        "ARGUS_AUTOPILOT_MODE": "ARMED",
        "ARGUS_AUTOMATION": "true",
        "ARGUS_ENABLE_LIVE_SUBMIT": "true",
        "ARGUS_ENABLE_TRACKR_LIVE": "true",
        "ARGUS_AUTOPILOT_SUBMIT": "true",
        "ARGUS_SWEEP_INTERVAL_HOURS": "0.01",
        "ARGUS_LIVE_DOMAIN_ALLOWLIST": "jobs.example.com",
        "ARGUS_DATA_DIR": "C:/private/live-data",
        "ARGUS_WATCHLIST_JSON": "C:/private/watchlist.json",
        "ARGUS_PROVIDER_PATH": "C:/private/provider.json",
        "ARGUS_API_TOKEN": "private-token",
        "ARGUS_PROFILE_PATH": "C:/private/profile.json",
        "LOCALAPPDATA": "C:/private/live-local-app-data",
        "APPDATA": "C:/private/live-app-data",
        "HTTPS_PROXY": "https://proxy.invalid",
        "SSLKEYLOGFILE": "C:/private/ssl.log",
        "TRACKR_COOKIE": "private-cookie",
        "BROWSER_PROFILE": "C:/private/browser-profile",
        "ARGUS_BROWSER_OPEN": "true",
        "ARGUS_BROWSER_HEADLESS": "false",
    }

    safe_root = tmp_path / "safe-runtime"
    environment = launcher._safe_child_environment(inherited, data_root=safe_root)

    assert environment["PATH"] == inherited["PATH"]
    assert environment["ARGUS_HOST"] == "127.0.0.1"
    assert environment["ARGUS_AUTOMATION_MODE"] == "OFF"
    assert environment["ARGUS_AUTOMATION_STATE"] == "OFF"
    assert environment["ARGUS_AUTOPILOT_MODE"] == "OFF"
    assert environment["ARGUS_AUTOMATION"] == "OFF"
    assert environment["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
    assert environment["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
    assert environment["ARGUS_AUTOPILOT_SUBMIT"] == "false"
    assert environment["ARGUS_SWEEP_INTERVAL_HOURS"] == "0"
    assert environment["ARGUS_LIVE_DOMAIN_ALLOWLIST"] == ""
    assert environment["ARGUS_DATA_DIR"] == str(safe_root.resolve())
    assert environment["LOCALAPPDATA"] == inherited["LOCALAPPDATA"]
    assert environment["APPDATA"] == inherited["APPDATA"]
    assert "ARGUS_WATCHLIST_JSON" not in environment
    assert "ARGUS_PROVIDER_PATH" not in environment
    assert "ARGUS_API_TOKEN" not in environment
    assert "ARGUS_PROFILE_PATH" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "SSLKEYLOGFILE" not in environment
    assert "TRACKR_COOKIE" not in environment
    assert "BROWSER_PROFILE" not in environment
    assert environment["ARGUS_BROWSER_OPEN"] == "true"
    assert environment["ARGUS_BROWSER_HEADLESS"] == "false"


class _LauncherHealthResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_LauncherHealthResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_launcher_health_requires_the_complete_safe_json_contract(monkeypatch) -> None:
    safe = {
        "status": "ok",
        "service": "ARGUS",
        "version": "0.2.0",
        "automation_mode": "OFF",
        "live_submit": False,
        "trackr_live": False,
    }
    monkeypatch.setattr(
        launcher,
        "_health_urlopen",
        lambda *_args, **_kwargs: _LauncherHealthResponse(
            200, json.dumps(safe).encode("utf-8")
        ),
    )

    assert launcher._healthy(8787) is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, b"{}"),
        (200, b"not-json"),
        (200, b'{"status":"ok","service":"ARGUS","version":""}'),
        (200, b'{"status":"ok","service":"ARGUS","version":"0.1.0","automation_mode":"OFF","live_submit":false,"trackr_live":false}'),
        (200, b'{"status":"ok","service":"other","version":"0.2.0"}'),
        (200, b'{"status":"ok","service":"ARGUS","version":"0.2.0","automation_mode":"ARMED","live_submit":false,"trackr_live":false}'),
        (200, b'{"status":"ok","service":"ARGUS","version":"0.2.0","automation_mode":"OFF","live_submit":true,"trackr_live":false}'),
        (200, b'{"status":"ok","service":"ARGUS","version":"0.2.0","automation_mode":"OFF","live_submit":false,"trackr_live":true}'),
        (200, b'{"status":"ok","service":"ARGUS","version":"0.2.0","automation_mode":"OFF","live_submit":false,"trackr_live":false,"extra":"must-reject"}'),
    ],
)
def test_launcher_health_rejects_unsafe_or_malformed_responses(
    monkeypatch, status: int, body: bytes
) -> None:
    monkeypatch.setattr(
        launcher,
        "_health_urlopen",
        lambda *_args, **_kwargs: _LauncherHealthResponse(status, body),
    )

    assert launcher._healthy(8787) is False


def test_launcher_health_refuses_http_redirects() -> None:
    redirected: list[str] = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path == "/healthz":
                self.send_response(302)
                self.send_header("Location", "/safe-healthz")
                self.end_headers()
                return
            redirected.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert launcher._healthy(server.server_port) is False
        assert redirected == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_launcher_default_data_root_ignores_inherited_localappdata_and_appdata(
    monkeypatch, tmp_path: Path
) -> None:
    trusted = tmp_path / "trusted-local" / "ARGUS"
    monkeypatch.setattr(launcher, "_trusted_runtime_data_dir", lambda: trusted)
    environment = launcher._safe_child_environment(
        {
            "LOCALAPPDATA": str(tmp_path / "stale-local"),
            "APPDATA": str(tmp_path / "stale-roaming"),
            "ARGUS_DATA_DIR": str(tmp_path / "stale-argus"),
        }
    )

    settings = Settings.load(environment)
    assert settings.data_dir == trusted.resolve()
    assert environment["LOCALAPPDATA"] == str(tmp_path / "stale-local")
    assert environment["APPDATA"] == str(tmp_path / "stale-roaming")


def test_launcher_explicit_data_root_requires_absolute_nonroot_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        launcher._safe_child_environment({}, data_root="relative-root")
    with pytest.raises(ValueError, match="filesystem root"):
        launcher._safe_child_environment({}, data_root=Path(tmp_path.anchor))


def test_launcher_in_process_environment_uses_same_sanitized_contract(
    monkeypatch, tmp_path: Path
) -> None:
    process_environment = {
        "ARGUS_WATCHLIST_JSON": "C:/private/watchlist.json",
        "ARGUS_AUTOMATION_MODE": "ARMED",
        "LOCALAPPDATA": "C:/stale-local",
    }
    monkeypatch.setattr(launcher.os, "environ", process_environment)
    safe = launcher._safe_child_environment(process_environment, data_root=tmp_path)

    launcher._apply_process_environment(safe)

    assert process_environment["ARGUS_AUTOMATION_MODE"] == "OFF"
    assert process_environment["ARGUS_DATA_DIR"] == str(tmp_path.resolve())
    assert "ARGUS_WATCHLIST_JSON" not in process_environment
    assert process_environment["LOCALAPPDATA"] == "C:/stale-local"


def test_launcher_source_main_passes_sanitized_environment_to_child(
    monkeypatch, tmp_path: Path
) -> None:
    safe_root = tmp_path / "source-safe"
    parent_environment = {
        "PATH": "C:/safe/bin",
        "ARGUS_AUTOMATION_MODE": "ARMED",
        "ARGUS_ENABLE_LIVE_SUBMIT": "true",
        "ARGUS_WATCHLIST_JSON": "C:/private/watchlist.json",
        "LOCALAPPDATA": "C:/stale-local",
    }
    monkeypatch.setattr(launcher.os, "environ", parent_environment)
    monkeypatch.setattr(launcher, "_trusted_runtime_data_dir", lambda: safe_root)
    monkeypatch.setattr(launcher, "_pick_port", lambda _configured: 45678)
    monkeypatch.setattr(launcher, "_app_root", lambda: tmp_path)

    class FakeLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.released = False

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            self.released = True

    monkeypatch.setattr(launcher, "RuntimeLock", FakeLock)
    captured: dict[str, object] = {}

    class FakeProcess:
        def wait(self) -> int:
            return 0

    def fake_popen(command, *, cwd, env):
        captured.update(command=command, cwd=cwd, env=env)
        return FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    assert launcher.main() == 0
    child_environment = captured["env"]
    assert isinstance(child_environment, dict)
    assert child_environment["ARGUS_AUTOMATION_MODE"] == "OFF"
    assert child_environment["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
    assert "ARGUS_WATCHLIST_JSON" not in child_environment
    assert child_environment["ARGUS_DATA_DIR"] == str(safe_root.resolve())
    assert captured["cwd"] == str(tmp_path)


def test_launcher_frozen_main_uses_the_same_safe_environment(
    monkeypatch, tmp_path: Path
) -> None:
    safe_root = tmp_path / "frozen-safe"
    parent_environment = {
        "ARGUS_AUTOMATION_STATE": "ARMED",
        "ARGUS_ENABLE_TRACKR_LIVE": "true",
        "ARGUS_PROVIDER_PATH": "C:/private/provider.json",
        "LOCALAPPDATA": "C:/stale-local",
    }
    monkeypatch.setattr(launcher.os, "environ", parent_environment)
    monkeypatch.setattr(launcher, "_trusted_runtime_data_dir", lambda: safe_root)
    monkeypatch.setattr(launcher, "_pick_port", lambda _configured: 45679)
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)

    class FakeLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

    monkeypatch.setattr(launcher, "RuntimeLock", FakeLock)
    run_arguments: dict[str, object] = {}

    class FakeUvicorn:
        @staticmethod
        def run(*args: object, **kwargs: object) -> None:
            run_arguments.update(args=args, kwargs=kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

    assert launcher.main() == 0
    assert run_arguments["kwargs"] == {
        "host": "127.0.0.1",
        "port": 45679,
        "log_level": "warning",
        "access_log": False,
    }
    assert parent_environment["ARGUS_AUTOMATION_MODE"] == "OFF"
    assert parent_environment["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
    assert "ARGUS_PROVIDER_PATH" not in parent_environment
    assert parent_environment["ARGUS_DATA_DIR"] == str(safe_root.resolve())


def test_launcher_playwright_cache_uses_trusted_localappdata(monkeypatch, tmp_path: Path) -> None:
    trusted = tmp_path / "trusted-local"
    (trusted / "ms-playwright" / "chromium-test").mkdir(parents=True)
    monkeypatch.setattr(launcher, "_trusted_local_app_data", lambda: trusted)

    assert launcher._playwright_browsers_env(
        {"LOCALAPPDATA": str(tmp_path / "stale-local")}
    ) == {"PLAYWRIGHT_BROWSERS_PATH": str(trusted / "ms-playwright")}


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "unexpected"])
def test_launcher_browser_open_is_opt_in_and_fails_closed(raw: str) -> None:
    assert launcher._browser_open_enabled({"ARGUS_BROWSER_OPEN": raw}) is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", " TrUe "])
def test_launcher_browser_open_accepts_only_explicit_true_values(raw: str) -> None:
    assert launcher._browser_open_enabled({"ARGUS_BROWSER_OPEN": raw}) is True


def test_launcher_browser_opener_never_touches_browser_when_disabled(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_healthy", lambda _port: True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    launcher._open_when_ready(8787, browser_open=False)

    assert opened == []


def test_launcher_browser_opener_uses_loopback_only_when_explicitly_enabled(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_healthy", lambda _port: True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    launcher._open_when_ready(8787, browser_open=True)

    assert opened == ["http://127.0.0.1:8787"]


def test_launcher_spec_collects_playwright_driver_and_version_resource() -> None:
    spec = (Path(__file__).resolve().parents[2] / "launcher.spec").read_text(
        encoding="utf-8"
    )

    assert 'collect_submodules("playwright")' in spec
    assert 'collect_data_files("playwright")' in spec
    assert 'PLAYWRIGHT_NODE = PLAYWRIGHT_DRIVER / ("node.exe" if sys.platform == "win32" else "node")' in spec
    assert 'binaries=PLAYWRIGHT_BINARIES' in spec
    assert 'VERSION_FILE = ROOT / "packaging" / "version_info.txt"' in spec
