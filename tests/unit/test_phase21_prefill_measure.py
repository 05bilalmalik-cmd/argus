from __future__ import annotations

import importlib.util
import ast
import hashlib
import importlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "phase21_prefill_measure.py"
APP_ID = "75db5b22-02c7-4cbf-967f-ec63a4b8655a"
OTHER_APP_ID = "94a2189c-75df-43b7-9dff-d02f28f51eb5"
SENTINEL_VALUE = "candidate-secret-value@argus.local"
PII_SENTINELS = (
    "Demo Candidate",
    "London Candidate Home",
    "".join(("+", "44", " ", "7700", " ", "900", "123")),
    "demo@argus.local",
)


def _harmless_nonreturning_tree_worker(send_connection, gate_connection, context):
    import subprocess
    import sys
    import time

    if not gate_connection.poll(5.0) or gate_connection.recv() is not True:
        return
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    Path(context["marker_path"]).write_text(str(child.pid), encoding="ascii")
    time.sleep(300)
    send_connection.close()


@pytest.fixture
def harness():
    assert SCRIPT.is_file(), "Phase 21 measurement harness has not been implemented"
    return importlib.import_module("scripts.phase21_prefill_measure")


def _create_database(
    path: Path,
    *,
    state: str = "NEEDS_USER",
    target: str = "APPLICATION_FORM",
    user_status: str = "NOT_APPLIED",
    applied_at: str | None = None,
    submission_reference: str = "",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE opportunities (
                id TEXT PRIMARY KEY,
                target_status TEXT NOT NULL,
                user_status TEXT NOT NULL
            );
            CREATE TABLE applications (
                id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
                state TEXT NOT NULL,
                applied_at TEXT,
                submission_reference TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE automation_runs (
                id TEXT PRIMARY KEY,
                application_id TEXT NOT NULL REFERENCES applications(id),
                mode TEXT NOT NULL,
                state TEXT NOT NULL,
                receipt_json TEXT NOT NULL DEFAULT '{}',
                trace_path TEXT NOT NULL DEFAULT '',
                screenshot_path TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE submission_intents (id TEXT PRIMARY KEY);
            CREATE TABLE submission_authorities (id TEXT PRIMARY KEY);
            CREATE TABLE submission_review_bindings (id TEXT PRIMARY KEY);
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY,
                epoch INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                details_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL DEFAULT '',
                event_hash TEXT NOT NULL
            );
            CREATE TABLE audit_chain_state (
                id INTEGER PRIMARY KEY,
                epoch INTEGER NOT NULL,
                head_event_id INTEGER,
                head_hash TEXT NOT NULL
            );
            CREATE TABLE audit_outbox (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE lab_submissions (id TEXT PRIMARY KEY);
            """
        )
        connection.execute(
            "INSERT INTO opportunities(id, target_status, user_status) VALUES ('opp', ?, ?)",
            (target, user_status),
        )
        connection.execute(
            "INSERT INTO applications"
            "(id, opportunity_id, state, applied_at, submission_reference) "
            "VALUES (?, 'opp', ?, ?, ?)",
            (APP_ID, state, applied_at, submission_reference),
        )
        connection.execute(
            "INSERT INTO audit_chain_state"
            "(id, epoch, head_event_id, head_hash) VALUES (1, 1, NULL, '')"
        )


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeJourney:
    action = SimpleNamespace(
        field=SimpleNamespace(
            selector=PII_SENTINELS[1],
            question=SimpleNamespace(
                label=PII_SENTINELS[0],
                name=PII_SENTINELS[2],
                selector=PII_SENTINELS[1],
                field_type="email",
                required=True,
            )
        ),
        mapping=SimpleNamespace(canonical_key=SimpleNamespace(value="contact.email")),
        value=PII_SENTINELS[3],
        source="approved_profile",
        status="resolved",
    )

    def __init__(self):
        self.last_plan = None
        self.step_index = 0

    def _build_plan(self, _fields, _evidence):
        return SimpleNamespace(actions=(self.action,))

    def _fill_resolved_action(self, _scope, _action):
        return None

    def _run_steps(self, _page):
        self.last_plan = self._build_plan([], {})
        self._fill_resolved_action(None, self.action)
        return {"state": "NEEDS_USER"}


class FakeNavigator:
    def __init__(self, client):
        self.client = client
        self.retry_calls: list[str] = []

    def retry_cleanup(self, session_id: str):
        self.retry_calls.append(session_id)
        if self.client.retry_succeeds:
            session = self.client.sessions[session_id]
            session["worker_alive"] = False
            session["cleanup_complete"] = True
        return SimpleNamespace()


class FakeClient:
    def __init__(
        self,
        database_path: Path,
        *,
        health_mode: str = "OFF",
        live_submit: bool = False,
        post_raises: bool = False,
        cancel_succeeds: bool = True,
        retry_succeeds: bool = True,
        unrelated_session: bool = False,
        cancel_raises: bool = False,
        submission_marker: str = "not_clicked",
        hide_owned_inventory: bool = False,
        no_session: bool = False,
        run_mode: str = "prefill",
        receipt_json: str = "{}",
        write_audit: bool = True,
        audit_epoch: int = 1,
        audit_entity_id: str = "run-1",
        audit_event_type: str = "automation.run_finished",
        post_raises_after_write: bool = False,
        audit_detail_mode: str = "prefill",
        audit_receipt_reference: str = "",
        audit_entity_type: str = "automation_run",
        second_finish_event: bool = False,
        extra_audit_type: str = "",
        after_write=None,
        audit_report_epoch: int = 1,
        run_state: str = "NEEDS_USER",
        response_state: str = "NEEDS_USER",
    ):
        self.database_path = database_path
        self.health_mode = health_mode
        self.live_submit = live_submit
        self.post_raises = post_raises
        self.cancel_succeeds = cancel_succeeds
        self.retry_succeeds = retry_succeeds
        self.cancel_raises = cancel_raises
        self.submission_marker = submission_marker
        self.hide_owned_inventory = hide_owned_inventory
        self.no_session = no_session
        self.run_mode = run_mode
        self.receipt_json = receipt_json
        self.write_audit = write_audit
        self.audit_epoch = audit_epoch
        self.audit_entity_id = audit_entity_id
        self.audit_event_type = audit_event_type
        self.post_raises_after_write = post_raises_after_write
        self.audit_detail_mode = audit_detail_mode
        self.audit_receipt_reference = audit_receipt_reference
        self.audit_entity_type = audit_entity_type
        self.second_finish_event = second_finish_event
        self.extra_audit_type = extra_audit_type
        self.after_write = after_write
        self.audit_report_epoch = audit_report_epoch
        self.run_state = run_state
        self.response_state = response_state
        self.calls: list[tuple[str, str, object]] = []
        self.sessions: dict[str, dict[str, object]] = {}
        self.post_count = 0
        if unrelated_session:
            self.sessions["unrelated-session"] = self._session(
                OTHER_APP_ID,
                "unrelated-session",
            )

    def _session(self, application_id: str, session_id: str) -> dict[str, object]:
        return {
            "session_id": session_id,
            "application_id": application_id,
            "mode": "prefill",
            "state": "HUMAN_REQUIRED",
            "status": "HUMAN_REQUIRED",
            "reason": "human boundary detected",
            "outcome": "human boundary detected",
            "summary": {},
            "manifest": {
                "first_party_requests": [
                    {
                        "host": "email-address-validator.us.greenhouse.io",
                        "path": "/address/validate?candidate=SECRET",
                        "method": "GET",
                        "resource_type": "fetch",
                        "manifest_vendor": "greenhouse",
                        "manifest_permission": True,
                        "delivery": "blocked",
                        "carries_candidate_data": True,
                        "candidate_data_kind": "email",
                        "body": SENTINEL_VALUE,
                    }
                ],
                "submission": self.submission_marker,
            },
            "worker_alive": True,
            "cleanup_complete": False,
            "headed": True,
            "visibility": "headed",
            "human_boundary": {"kind": "captcha", "raw": SENTINEL_VALUE},
            "raw_url": "https://example.invalid/?candidate=SECRET",
        }

    def get(self, url: str):
        self.calls.append(("GET", url, None))
        if url == "/healthz":
            return FakeResponse(
                200,
                {
                    "status": "ok",
                    "service": "ARGUS",
                    "version": "test",
                    "automation_mode": self.health_mode,
                    "live_submit": self.live_submit,
                    "trackr_live": False,
                },
            )
        if url == "/api/handoff/sessions":
            sessions = list(self.sessions.values())
            if self.hide_owned_inventory:
                sessions = [item for item in sessions if item["application_id"] != APP_ID]
            return FakeResponse(200, sessions)
        prefix = "/api/handoff/sessions/"
        if url.startswith(prefix):
            session_id = url[len(prefix):]
            return FakeResponse(200, self.sessions[session_id])
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, *, json=None):
        self.calls.append(("POST", url, json))
        if url == f"/api/applications/{APP_ID}/run?mode=prefill&headed=true":
            self.post_count += 1
            journey = FakeJourney()
            journey._run_steps(None)
            if not self.no_session:
                self.sessions["owned-session"] = self._session(APP_ID, "owned-session")
            if self.post_raises and not self.post_raises_after_write:
                raise RuntimeError("provider exception " + " ".join(PII_SENTINELS))
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    "INSERT INTO automation_runs"
                    "(id, application_id, mode, state, receipt_json) "
                    "VALUES ('run-1', ?, ?, ?, ?)",
                    (APP_ID, self.run_mode, self.run_state, self.receipt_json),
                )
                if self.write_audit:
                    details = {
                        "application_id": APP_ID,
                        "mode": self.audit_detail_mode,
                        "receipt_reference": self.audit_receipt_reference,
                        "first_party_requests": self._session(APP_ID, "evidence")[
                            "manifest"
                        ]["first_party_requests"],
                    }
                    connection.execute(
                        "INSERT INTO audit_events"
                        "(epoch, created_at, actor, event_type, entity_type, entity_id, "
                        "details_json, previous_hash, event_hash) "
                        "VALUES (?, '2026-08-28T00:00:00Z', 'automation', ?, "
                        "?, ?, ?, '', 'event-hash-1')",
                        (
                            self.audit_epoch,
                            self.audit_event_type,
                            self.audit_entity_type,
                            self.audit_entity_id,
                            __import__("json").dumps(details),
                        ),
                    )
                    if self.second_finish_event:
                        connection.execute(
                            "INSERT INTO audit_events"
                            "(epoch, created_at, actor, event_type, entity_type, entity_id, "
                            "details_json, previous_hash, event_hash) "
                            "VALUES (?, '2026-08-28T00:00:01Z', 'automation', "
                            "'automation.run_finished', 'automation_run', 'run-1', ?, "
                            "'', 'event-hash-2')",
                            (self.audit_epoch, __import__("json").dumps(details)),
                        )
                    if self.extra_audit_type:
                        connection.execute(
                            "INSERT INTO audit_events"
                            "(epoch, created_at, actor, event_type, entity_type, entity_id, "
                            "details_json, previous_hash, event_hash) "
                            "VALUES (?, '2026-08-28T00:00:02Z', 'automation', ?, "
                            "'automation_run', 'run-1', '{}', '', 'event-hash-3')",
                            (self.audit_epoch, self.extra_audit_type),
                        )
                if self.after_write is not None:
                    self.after_write(connection)
            if self.post_raises:
                raise RuntimeError("provider exception " + " ".join(PII_SENTINELS))
            return FakeResponse(
                200,
                {
                    "run_id": "run-1",
                    "state": self.response_state,
                    "risk_level": 3,
                    "adapter": "greenhouse",
                    "blocked_reasons": ["human_boundary"],
                    "receipt": None,
                    "trace_path": "",
                    "screenshot_path": "",
                    "session_id": "" if self.no_session else "owned-session",
                    "handoff_session_id": "" if self.no_session else "owned-session",
                    "human_boundary": {"kind": "captcha", "raw": SENTINEL_VALUE},
                },
            )
        cancel_prefix = "/api/handoff/sessions/"
        cancel_suffix = "/cancel"
        if url.startswith(cancel_prefix) and url.endswith(cancel_suffix):
            session_id = url[len(cancel_prefix):-len(cancel_suffix)]
            assert json == {"application_id": self.sessions[session_id]["application_id"]}
            if self.cancel_raises:
                raise RuntimeError("cancel response lost")
            if self.cancel_succeeds:
                self.sessions[session_id]["worker_alive"] = False
                self.sessions[session_id]["cleanup_complete"] = True
                self.sessions[session_id]["state"] = "CANCELLED"
            return FakeResponse(200, self.sessions[session_id])
        raise AssertionError(f"unexpected POST {url}")


class FakeRuntime:
    def __init__(self, client: FakeClient):
        self.client = client
        self.navigator = FakeNavigator(client)
        self.journey_class = FakeJourney
        self.database_path = client.database_path
        self.traces_dir = client.database_path.parent / "traces"
        self.screenshots_dir = client.database_path.parent / "screenshots"

    def audit_check(self):
        return {
            "valid": True,
            "complete": True,
            "state_consistent": True,
            "pending_events": 0,
            "epoch": self.client.audit_report_epoch,
            "checked_events": 0,
        }


def _runtime_factory(runtime: FakeRuntime):
    @contextmanager
    def factory(_data_dir):
        yield runtime

    return factory


def _run(harness, tmp_path: Path, client: FakeClient | None = None):
    database_path = tmp_path / "argus.db"
    if not database_path.exists():
        _create_database(database_path)
    client = client or FakeClient(database_path)
    runtime = FakeRuntime(client)
    env = {
        "ARGUS_DATA_DIR": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }
    result = harness.execute_measurement(
        APP_ID,
        env=env,
        runtime_factory=_runtime_factory(runtime),
        poll_interval=0,
        cleanup_timeout=0,
    )
    return result, client, runtime, env


def test_plan_and_invalid_id_never_touch_database_or_import_app(harness, monkeypatch, capsys):
    touched = []
    monkeypatch.setattr(harness, "backup_database", lambda *a, **k: touched.append("db"))
    monkeypatch.setattr(harness, "_open_runtime", lambda *a, **k: touched.append("app"))

    assert harness.main(["--application-id", APP_ID]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan == {
        "application_id": APP_ID,
        "execute": False,
        "operation": "phase21_prefill_measurement_plan",
        "request": f"/api/applications/{APP_ID}/run?mode=prefill&headed=true",
    }
    with pytest.raises(SystemExit) as invalid:
        harness.main(["--application-id", "not-approved"])
    assert invalid.value.code == 2
    assert touched == []


def test_online_backup_is_wal_consistent_and_verified(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    writer = sqlite3.connect(database_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, marker TEXT NOT NULL)")
    writer.commit()
    writer.execute("INSERT INTO evidence(marker) VALUES ('committed-in-wal')")
    writer.commit()
    backup_dir = tmp_path / "backups"
    try:
        bundle = harness.backup_database(database_path, backup_dir, APP_ID)
    finally:
        writer.close()

    with sqlite3.connect(bundle.path) as copy:
        assert copy.execute("SELECT marker FROM evidence").fetchone() == ("committed-in-wal",)
    assert bundle.evidence["integrity_check"] == "ok"
    assert bundle.evidence["foreign_key_violations"] == 0
    assert bundle.evidence["bytes"] > 0
    assert len(bundle.evidence["sha256"]) == 64
    assert bundle.evidence["filename"].startswith("argus-phase21-pre-prefill-")


def test_backup_happens_before_runtime_import_and_post(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path)
    runtime = FakeRuntime(client)
    events = []

    def backup(*args, **kwargs):
        events.append("backup")
        return harness.backup_database(*args, **kwargs)

    @contextmanager
    def runtime_factory(_data_dir):
        events.append("runtime")
        yield runtime

    original_post = client.post

    def post(*args, **kwargs):
        events.append("post")
        return original_post(*args, **kwargs)

    client.post = post
    result = harness.execute_measurement(
        APP_ID,
        env={"ARGUS_DATA_DIR": str(tmp_path), "LOCALAPPDATA": str(tmp_path)},
        backup_creator=backup,
        runtime_factory=runtime_factory,
        poll_interval=0,
        cleanup_timeout=0,
    )
    assert result["ok"] is True
    assert events[:3] == ["backup", "runtime", "post"]


def test_backup_failure_blocks_runtime_and_post(harness, tmp_path):
    called = []

    def broken_backup(*_args, **_kwargs):
        raise sqlite3.DatabaseError("integrity failure")

    @contextmanager
    def runtime_factory(_data_dir):
        called.append("runtime")
        yield None

    result = harness.execute_measurement(
        APP_ID,
        env={"ARGUS_DATA_DIR": str(tmp_path), "LOCALAPPDATA": str(tmp_path)},
        backup_creator=broken_backup,
        runtime_factory=runtime_factory,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "backup_failed"
    assert called == []


def test_corrupt_source_integrity_blocks_runtime(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    database_path.write_bytes(b"this is not a SQLite database")
    called = []

    @contextmanager
    def runtime_factory(_data_dir):
        called.append("runtime")
        yield None

    result = harness.execute_measurement(
        APP_ID,
        env={"ARGUS_DATA_DIR": str(tmp_path), "LOCALAPPDATA": str(tmp_path)},
        runtime_factory=runtime_factory,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "backup_failed"
    assert called == []


def test_exact_prefill_headed_request_env_and_no_retry(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path)
    runtime = FakeRuntime(client)
    env = {"ARGUS_DATA_DIR": str(tmp_path), "LOCALAPPDATA": str(tmp_path)}

    @contextmanager
    def runtime_factory(_data_dir):
        assert env["ARGUS_AUTOMATION_MODE"] == "OFF"
        assert env["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
        assert env["ARGUS_ENABLE_APPLY_CLICK"] == "false"
        assert env["ARGUS_SWEEP_INTERVAL_HOURS"] == "0"
        assert env["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
        assert env["ARGUS_ENABLE_NOTIFICATIONS"] == "false"
        yield runtime

    result = harness.execute_measurement(
        APP_ID,
        env=env,
        runtime_factory=runtime_factory,
        poll_interval=0,
        cleanup_timeout=0,
    )
    assert result["ok"] is True
    run_calls = [call for call in client.calls if call[0] == "POST" and "/run?" in call[1]]
    assert run_calls == [
        ("POST", f"/api/applications/{APP_ID}/run?mode=prefill&headed=true", None)
    ]
    assert client.post_count == 1


@pytest.mark.parametrize(
    ("health_mode", "live_submit", "state", "target", "user_status", "error_code"),
    [
        ("ARMED", False, "NEEDS_USER", "APPLICATION_FORM", "NOT_APPLIED", "unsafe_health"),
        ("OFF", True, "NEEDS_USER", "APPLICATION_FORM", "NOT_APPLIED", "unsafe_health"),
        ("OFF", False, "QUEUED", "APPLICATION_FORM", "NOT_APPLIED", "application_drift"),
        ("OFF", False, "NEEDS_USER", "JOB_DETAIL", "NOT_APPLIED", "application_drift"),
        ("OFF", False, "NEEDS_USER", "APPLICATION_FORM", "APPLIED", "application_drift"),
    ],
)
def test_health_and_application_drift_block_the_post(
    harness,
    tmp_path,
    health_mode,
    live_submit,
    state,
    target,
    user_status,
    error_code,
):
    database_path = tmp_path / "argus.db"
    _create_database(database_path, state=state, target=target, user_status=user_status)
    client = FakeClient(database_path, health_mode=health_mode, live_submit=live_submit)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["error"]["code"] == error_code
    assert client.post_count == 0


def test_existing_exact_application_session_blocks_without_cancelling(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path)
    client.sessions["pre-existing"] = client._session(APP_ID, "pre-existing")
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "live_session_exists"
    assert client.post_count == 0
    assert not any(call[1].endswith("/cancel") for call in client.calls)


def test_instrumentation_never_captures_action_value_and_output_is_safe(harness, tmp_path):
    result, _client, _runtime, _env = _run(harness, tmp_path)
    encoded = json.dumps(result, sort_keys=True)
    assert result["ok"] is True
    assert result["measurement"]["successful_fill_count"] == 1
    assert result["measurement"]["before_filled_count"] == 1
    assert result["measurement"]["after_filled_count"] == 1
    assert result["measurement"]["fields"][0] == {
        "canonical_key": "contact.email",
        "control_category": "email",
        "ordinal": "step-1-field-1",
        "required": True,
        "source": "approved_profile",
        "status": "resolved",
    }
    assert SENTINEL_VALUE not in encoded
    assert "candidate=SECRET" not in encoded
    assert "raw_url" not in encoded
    assert result["session"]["human_boundary_kind"] == "captcha"
    assert result["session"]["first_party_requests"][0]["path"] == "/address/validate"
    assert result["submission"] == "not_clicked"


def test_cleanup_cancels_only_owned_application_session(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, unrelated_session=True)
    result, client, runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is True
    cancel_calls = [call for call in client.calls if call[1].endswith("/cancel")]
    assert cancel_calls == [
        (
            "POST",
            "/api/handoff/sessions/owned-session/cancel",
            {"application_id": APP_ID},
        )
    ]
    assert client.sessions["unrelated-session"]["worker_alive"] is True
    assert runtime.navigator.retry_calls == []
    assert result["cleanup"] == {
        "attempted": True,
        "cleanup_complete": True,
        "retry_cleanup_used": False,
        "worker_alive": False,
    }


def test_cleanup_uncertainty_fails_closed_after_one_same_manager_retry(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, cancel_succeeds=False, retry_succeeds=False)
    result, client, runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "cleanup_uncertain"
    assert client.post_count == 1
    assert runtime.navigator.retry_calls == ["owned-session"]
    assert result["cleanup"]["cleanup_complete"] is False
    assert result["cleanup"]["worker_alive"] is True


def test_cancel_transport_error_still_uses_one_same_manager_cleanup_retry(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, cancel_raises=True, retry_succeeds=True)
    result, client, runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is True
    assert client.post_count == 1
    assert runtime.navigator.retry_calls == ["owned-session"]
    assert result["cleanup"] == {
        "attempted": True,
        "cleanup_complete": True,
        "retry_cleanup_used": True,
        "worker_alive": False,
    }


def test_response_session_inventory_mismatch_still_cancels_exact_response_id(
    harness,
    tmp_path,
):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, hide_owned_inventory=True)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "session_binding_mismatch"
    assert (
        "POST",
        "/api/handoff/sessions/owned-session/cancel",
        {"application_id": APP_ID},
    ) in client.calls
    assert client.sessions["owned-session"]["cleanup_complete"] is True


def test_post_exception_is_not_retried_and_owned_session_is_cleaned(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, post_raises=True)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["error"] == {
        "code": "runtime_error",
        "exception_type": "RuntimeError",
    }
    assert client.post_count == 1
    assert client.sessions["owned-session"]["cleanup_complete"] is True
    assert SENTINEL_VALUE not in json.dumps(result)


def test_request_path_surface_contains_no_unsafe_operation(harness, tmp_path):
    result, client, _runtime, _env = _run(harness, tmp_path)
    assert result["ok"] is True
    api_calls = [
        (method, url)
        for method, url, _body in client.calls
        if url.startswith("/api/")
    ]
    assert api_calls == [
        ("GET", "/api/handoff/sessions"),
        ("POST", f"/api/applications/{APP_ID}/run?mode=prefill&headed=true"),
        ("GET", "/api/handoff/sessions"),
        ("GET", "/api/handoff/sessions/owned-session"),
        ("POST", "/api/handoff/sessions/owned-session/cancel"),
        ("GET", "/api/handoff/sessions/owned-session"),
    ]


def test_projector_rejects_unmeasured_host_path_and_non_not_clicked_manifest(harness, tmp_path):
    unmeasured = [
        {
            "host": "email-address-validator.us.greenhouse.io",
            "path": "/candidate/private-slug",
            "method": "GET",
            "resource_type": "fetch",
            "manifest_vendor": "greenhouse",
            "manifest_permission": True,
            "delivery": "blocked",
            "carries_candidate_data": True,
            "candidate_data_kind": "email",
        }
    ]
    assert harness._project_first_party(unmeasured) == []

    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, submission_marker="unexpected")
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["error"]["code"] == "session_invariant_failed"
    assert client.sessions["owned-session"]["cleanup_complete"] is True


def test_instrumentation_restores_both_methods_after_exception(harness):
    original_fill = FakeJourney._fill_resolved_action
    original_build = FakeJourney._build_plan
    with pytest.raises(RuntimeError):
        with harness._instrument_journey(FakeJourney, harness._FieldCapture()):
            raise RuntimeError("test interruption")
    assert FakeJourney._fill_resolved_action is original_fill
    assert FakeJourney._build_plan is original_build


def test_run_and_database_invariants_are_reported(harness, tmp_path):
    result, _client, _runtime, _env = _run(harness, tmp_path)
    assert result["ok"] is True
    assert result["invariants"]["run"] == {
        "exactly_one_new_run": True,
        "response_run_is_new": True,
        "application_id_matches": True,
        "mode": "prefill",
        "receipt_empty": True,
        "state_nonterminal": True,
        "response_state_matches_run": True,
        "run_state_matches_application": True,
        "no_submit_mode_run_added": True,
    }
    assert result["invariants"]["database"]["integrity_check"] == "ok"
    assert result["invariants"]["database"]["foreign_key_violations"] == 0
    assert result["invariants"]["database"]["deleted_rows"] == 0
    assert result["invariants"]["database"]["automation_runs_added"] == 1
    assert result["invariants"]["database"]["submission_intents_added"] == 0
    assert result["invariants"]["database"]["submission_authorities_added"] == 0
    assert result["invariants"]["audit"]["valid"] is True
    assert result["invariants"]["audit"]["complete"] is True


def test_cli_prints_exactly_one_json_result(harness, monkeypatch, capsys):
    expected = {"ok": True, "application_id": APP_ID}
    monkeypatch.setattr(harness, "_run_public_controller", lambda *a, **k: expected)
    assert harness.main(["--application-id", APP_ID, "--execute"]) == 0
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output) == expected


def test_static_import_and_request_literals_are_fail_closed():
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name == "app"
        or name.startswith("app.")
        or name in {"fastapi", "sqlalchemy"}
        for name in top_level_imports
    )
    forbidden = (
        "mode=" + "submit",
        "/con" + "firm",
        "/cont" + "inue",
        "/final-" + "manifest",
    )
    assert not any(item in source for item in forbidden)


# Hostile-review RED contract: these tests intentionally describe the safer
# boundary before the implementation is changed.
def test_review_red_submission_requires_zero_baseline_and_semantic_audit(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO submission_intents(id) VALUES ('historic-intent')")
    client = FakeClient(database_path)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert client.post_count == 0
    assert result["submission"] == "unknown"


def test_review_red_serialized_fields_use_only_generated_ordinals(harness, tmp_path):
    result, _client, _runtime, _env = _run(harness, tmp_path)
    encoded = json.dumps(result, sort_keys=True)
    assert "Email address" not in encoded
    assert "candidate[email]" not in encoded
    assert result["measurement"]["fields"][0]["ordinal"] == "step-1-field-1"
    field_keys = result["measurement"]["fields"][0].keys()
    assert not ({"identity", "label", "name", "selector"} & field_keys)


def test_review_red_instruments_every_plan_and_restores_exact_methods(harness):
    assert hasattr(FakeJourney, "_build_plan")
    original_build = FakeJourney._build_plan
    original_fill = FakeJourney._fill_resolved_action
    capture = harness._FieldCapture()
    with harness._instrument_journey(FakeJourney, capture):
        assert FakeJourney._build_plan is not original_build
        assert FakeJourney._fill_resolved_action is not original_fill
    assert FakeJourney._build_plan is original_build
    assert FakeJourney._fill_resolved_action is original_fill


def test_review_red_public_cli_rejects_data_dir_override(
    harness, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path / "other"))
    touched = []
    monkeypatch.setattr(harness, "backup_database", lambda *a, **k: touched.append("backup"))
    assert harness.main(["--application-id", APP_ID, "--execute"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "noncanonical_data_directory"
    assert result["submission"] == "unknown"
    assert touched == []


def test_review_red_parent_controller_deadline_fails_closed(harness, tmp_path):
    assert hasattr(harness, "_run_public_controller")
    assert hasattr(harness, "_WindowsJobController")
    assert harness.PHASE21_TOTAL_DEADLINE_SECONDS > 0


@pytest.mark.parametrize("table", (
    "submission_intents",
    "submission_authorities",
    "submission_review_bindings",
    "lab_submissions",
))
def test_every_submission_table_must_be_globally_empty_before_post(harness, tmp_path, table):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f'INSERT INTO "{table}"(id) VALUES (?)', (f"seed-{table}",))
    client = FakeClient(database_path)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["submission"] == "unknown"
    assert result["error"]["code"] == "submission_baseline_not_empty"
    assert client.post_count == 0


@pytest.mark.parametrize(
    ("applied_at", "reference"),
    (("2026-08-28T00:00:00Z", ""), (None, "provider-reference")),
)
def test_application_submission_markers_block_before_post(
    harness, tmp_path, applied_at, reference
):
    database_path = tmp_path / "argus.db"
    _create_database(
        database_path,
        applied_at=applied_at,
        submission_reference=reference,
    )
    client = FakeClient(database_path)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["submission"] == "unknown"
    assert result["error"]["code"] == "application_drift"
    assert client.post_count == 0


@pytest.mark.parametrize(
    "client_kwargs",
    (
        {"run_mode": "submit"},
        {"receipt_json": '{"reference":"provider-reference"}'},
        {"write_audit": False},
        {"audit_epoch": 2},
        {"audit_entity_id": "different-run"},
        {"audit_entity_type": "application"},
        {"audit_detail_mode": "submit"},
        {"audit_receipt_reference": "provider-reference"},
        {"second_finish_event": True},
        {"extra_audit_type": "submission.intent_created"},
        {"audit_report_epoch": 2},
    ),
)
def test_submission_truth_requires_exact_run_and_current_epoch_audit_proof(
    harness, tmp_path, client_kwargs
):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, **client_kwargs)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert client.post_count == 1
    assert result["ok"] is False
    assert result["submission"] == "unknown"
    assert result["error"]["code"] == "invariant_failed"


@pytest.mark.parametrize(
    "mutation",
    (
        lambda connection: connection.execute(
            "UPDATE applications SET state = 'SUBMITTED' WHERE id = ?", (APP_ID,)
        ),
        lambda connection: connection.execute(
            "UPDATE applications SET applied_at = '2026-08-28T00:00:00Z' WHERE id = ?",
            (APP_ID,),
        ),
        lambda connection: connection.execute(
            "UPDATE applications SET submission_reference = 'provider-reference' WHERE id = ?",
            (APP_ID,),
        ),
        lambda connection: connection.execute(
            "INSERT INTO submission_intents(id) VALUES ('new-intent')"
        ),
    ),
)
def test_post_application_and_submission_mutations_keep_truth_unknown(
    harness, tmp_path, mutation
):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, after_write=mutation)
    result, _client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["submission"] == "unknown"
    assert result["error"]["code"] == "invariant_failed"


def test_deletion_after_post_is_detected(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO audit_outbox(id, status) VALUES (9, 'DONE')")
    client = FakeClient(
        database_path,
        after_write=lambda connection: connection.execute(
            "DELETE FROM audit_outbox WHERE id = 9"
        ),
    )
    result, _client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["invariants"]["database"]["deleted_rows"] == 1
    assert result["submission"] == "unknown"
    assert result["ok"] is False


def test_post_exception_still_runs_best_effort_invariants_and_never_claims(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, post_raises=True, post_raises_after_write=True)
    result, client, _runtime, _env = _run(harness, tmp_path, client)
    assert client.post_count == 1
    assert result["error"] == {"code": "runtime_error", "exception_type": "RuntimeError"}
    assert result["submission"] == "unknown"
    assert result["invariants"]["run"]["exactly_one_new_run"] is True
    assert result["invariants"]["semantic"]["exact_bound_current_epoch_run_finished"] is True
    assert client.sessions["owned-session"]["cleanup_complete"] is True
    assert not any(sentinel in json.dumps(result) for sentinel in PII_SENTINELS)


def test_no_session_uses_only_exact_run_finished_first_party_evidence(harness, tmp_path):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    client = FakeClient(database_path, no_session=True)
    result, _client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is True
    assert result["submission"] == "not_clicked"
    assert "session" not in result
    assert result["first_party_requests"] == [
        {
            "host": "email-address-validator.us.greenhouse.io",
            "path": "/address/validate",
            "method": "GET",
            "resource_type": "fetch",
            "manifest_vendor": "greenhouse",
            "manifest_permission": True,
            "delivery": "blocked",
            "carries_candidate_data": True,
            "candidate_data_kind": "email",
        }
    ]


def test_plain_pii_sentinels_never_serialize_from_any_action_or_error_surface(
    harness, tmp_path
):
    success, _client, _runtime, _env = _run(harness, tmp_path)
    failed_db = tmp_path / "failed" / "argus.db"
    failed_db.parent.mkdir()
    _create_database(failed_db)
    failed_client = FakeClient(failed_db, post_raises=True)
    failed, _client, _runtime, _env = _run(harness, failed_db.parent, failed_client)
    encoded = json.dumps({"success": success, "failed": failed}, sort_keys=True)
    for sentinel in (*PII_SENTINELS, SENTINEL_VALUE):
        assert sentinel not in encoded
    for field in success["measurement"]["fields"]:
        assert not ({"identity", "label", "name", "selector", "value", "error"} & field.keys())


def _action(
    key: str,
    status: str = "resolved",
    *,
    selector: str = PII_SENTINELS[1],
    name: str = PII_SENTINELS[2],
    label: str = PII_SENTINELS[0],
    source: str = "approved_profile",
):
    return SimpleNamespace(
        field=SimpleNamespace(
            selector=selector,
            question=SimpleNamespace(
                label=label,
                name=name,
                field_type="text",
                required=True,
            )
        ),
        mapping=SimpleNamespace(canonical_key=SimpleNamespace(value=key)),
        value=PII_SENTINELS[3],
        source=source,
        status=status,
    )


def test_multi_step_repeated_plans_and_repeated_fills_are_deduplicated(harness):
    class Journey:
        def __init__(self):
            self.step_index = 0
            self.next_actions = ()

        def _build_plan(self, _fields, _evidence):
            return SimpleNamespace(actions=self.next_actions)

        def _fill_resolved_action(self, _scope, _action_value):
            return None

    capture = harness._FieldCapture()
    with harness._instrument_journey(Journey, capture):
        journey = Journey()
        first_plan = (_action("identity.first_name"), _action("identity.last_name"))
        journey.next_actions = first_plan
        journey._build_plan([], {})
        journey._fill_resolved_action(None, first_plan[0])
        journey._fill_resolved_action(None, first_plan[0])
        repeated_plan = (_action("identity.first_name"), _action("identity.last_name"))
        journey.next_actions = repeated_plan
        journey._build_plan([], {})
        journey._fill_resolved_action(None, repeated_plan[0])
        journey.step_index = 1
        second_plan = (_action("contact.email"), _action("unknown", "blocked"))
        journey.next_actions = second_plan
        journey._build_plan([], {})
        journey._fill_resolved_action(None, second_plan[0])
    payload = capture.payload()
    assert [field["ordinal"] for field in payload["fields"]] == [
        "step-1-field-1",
        "step-1-field-2",
        "step-2-field-1",
        "step-2-field-2",
    ]
    assert payload["before_filled_count"] == 3
    assert payload["after_filled_count"] == 2
    assert payload["failed_fields"] == []
    assert payload["unfilled"] == [
        {"ordinal": "step-1-field-2", "reason": "not_attempted"},
        {"ordinal": "step-2-field-2", "reason": "plan_blocked"},
    ]


def test_changed_safe_field_at_same_position_is_retained_and_failed_retry_can_succeed(harness):
    class Journey:
        def __init__(self):
            self.step_index = 0
            self.actions = ()
            self.fail = True

        def _build_plan(self, _fields, _evidence):
            return SimpleNamespace(actions=self.actions)

        def _fill_resolved_action(self, _scope, _action_value):
            if self.fail:
                self.fail = False
                raise RuntimeError("fill failure " + " ".join(PII_SENTINELS))

    capture = harness._FieldCapture()
    with harness._instrument_journey(Journey, capture):
        journey = Journey()
        first = _action("identity.first_name")
        journey.actions = (first,)
        journey._build_plan([], {})
        with pytest.raises(RuntimeError):
            journey._fill_resolved_action(None, first)
        journey._fill_resolved_action(None, first)
        changed = _action("contact.email")
        journey.actions = (changed,)
        journey._build_plan([], {})
    payload = capture.payload()
    assert [field["ordinal"] for field in payload["fields"]] == [
        "step-1-field-1",
        "step-1-field-2",
    ]
    assert payload["after_filled_count"] == 1
    assert payload["failed_fields"] == []
    assert payload["unfilled"] == [
        {"ordinal": "step-1-field-2", "reason": "not_attempted"}
    ]


def test_distinct_replacement_control_same_projection_gets_new_private_ordinal(harness):
    class Journey:
        step_index = 0

        def __init__(self):
            self.actions = ()

        def _build_plan(self, _fields, _evidence):
            return SimpleNamespace(actions=self.actions)

        def _fill_resolved_action(self, _scope, _action_value):
            return None

    first_selector = "#candidate-location-control"
    replacement_selector = "#candidate-location-control-replacement"
    repeated = _action("contact.city", selector=first_selector)
    replacement = _action("contact.city", selector=replacement_selector)
    capture = harness._FieldCapture()
    with harness._instrument_journey(Journey, capture):
        journey = Journey()
        journey.actions = (repeated,)
        journey._build_plan([], {})
        journey.actions = (_action("contact.city", selector=first_selector),)
        journey._build_plan([], {})
        journey.actions = (replacement,)
        journey._build_plan([], {})
    payload = capture.payload()
    assert [field["ordinal"] for field in payload["fields"]] == [
        "step-1-field-1",
        "step-1-field-2",
    ]
    encoded = json.dumps(payload, sort_keys=True)
    for raw in (first_selector, replacement_selector, PII_SENTINELS[0], PII_SENTINELS[2]):
        assert raw not in encoded
        assert hashlib.sha256(raw.encode("utf-8")).hexdigest() not in encoded


@pytest.mark.parametrize(
    ("client_kwargs", "mutation"),
    (
        ({}, "UPDATE opportunities SET user_status = 'APPLIED' WHERE id = 'opp'"),
        ({}, "UPDATE opportunities SET target_status = 'JOB_DETAIL' WHERE id = 'opp'"),
        ({"response_state": "FILLING"}, ""),
        ({"run_state": "FILLING"}, ""),
        ({}, "UPDATE applications SET state = 'READY_TO_SUBMIT' WHERE id = '" + APP_ID + "'"),
    ),
)
def test_final_opportunity_and_exact_three_way_state_binding(
    harness,
    tmp_path,
    client_kwargs,
    mutation,
):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    callback = (
        (lambda connection: connection.execute(mutation))
        if mutation
        else None
    )
    client = FakeClient(database_path, after_write=callback, **client_kwargs)
    result, _client, _runtime, _env = _run(harness, tmp_path, client)
    assert result["ok"] is False
    assert result["submission"] == "unknown"
    assert result["error"]["code"] == "invariant_failed"


def test_worker_failure_lifecycle_never_invents_cleanup_or_browser_facts(harness):
    rejected = harness._worker_failure(
        APP_ID,
        "worker_parent_context_invalid",
        cleanup_attempted=False,
        cleanup_complete=None,
        worker_alive=False,
        process_tree_terminated=False,
    )
    assert rejected["cleanup"] == {
        "attempted": False,
        "cleanup_complete": None,
        "retry_cleanup_used": False,
        "worker_alive": False,
        "process_tree_terminated": False,
    }
    deadline = harness._worker_failure(
        APP_ID,
        "deadline_exceeded",
        cleanup_attempted=None,
        cleanup_complete=False,
        worker_alive=None,
        process_tree_terminated=True,
    )
    assert deadline["cleanup"] == {
        "attempted": None,
        "cleanup_complete": False,
        "retry_cleanup_used": False,
        "worker_alive": None,
        "process_tree_terminated": True,
    }


def test_public_controller_backs_up_canonical_path_before_timeout_result(harness, tmp_path):
    data_dir = tmp_path / "local" / "ARGUS"
    data_dir.mkdir(parents=True)
    _create_database(data_dir / "argus.db")

    class BackupController:
        def run(self, context, _timeout):
            bundle = harness._backup_database_to_path(
                Path(context["database_path"]),
                Path(context["backup_path"]),
                APP_ID,
                partial_path=Path(context["backup_partial_path"]),
            )
            return {
                "ok": True,
                "application_id": APP_ID,
                "backup_path": str(bundle.path),
                "backup": bundle.evidence,
            }

    class DeadlineController:
        def __init__(self):
            self.calls = []

        def run(self, context, timeout):
            backup_path = Path(context["backup_path"])
            assert backup_path.is_file()
            assert backup_path.parent == data_dir / "backups"
            self.calls.append((context, timeout))
            result = harness._worker_failure(
                APP_ID,
                "deadline_exceeded",
                backup=context["backup"],
                cleanup_attempted=None,
                cleanup_complete=False,
                worker_alive=None,
                process_tree_terminated=True,
            )
            return result

    controller = DeadlineController()
    result = harness._run_public_controller(
        APP_ID,
        env={"LOCALAPPDATA": str(tmp_path / "local")},
        backup_controller=BackupController(),
        controller=controller,
        deadline=5.0,
    )
    assert len(controller.calls) == 1
    assert result["submission"] == "unknown"
    assert result["cleanup"]["cleanup_complete"] is False
    assert result["cleanup"]["process_tree_terminated"] is True
    assert result["error"]["code"] == "deadline_exceeded"


def test_hidden_worker_rejects_missing_parent_token_before_runtime(harness, monkeypatch):
    touched = []
    monkeypatch.delenv(harness._WORKER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        harness,
        "_execute_verified_backup",
        lambda *a, **k: touched.append(True),
    )

    class Gate:
        def poll(self, timeout):
            return True

        def recv(self):
            return True

        def close(self):
            return None

    class Sender:
        def __init__(self):
            self.result = None

        def send(self, result):
            self.result = result

        def close(self):
            return None

    sender = Sender()
    harness._worker_entry(sender, Gate(), {"application_id": APP_ID})
    assert touched == []
    assert sender.result["error"]["code"] == "worker_parent_context_invalid"
    assert sender.result["submission"] == "unknown"


def test_public_backup_outside_exact_backups_child_is_rejected(harness, tmp_path):
    data_dir = tmp_path / "local" / "ARGUS"
    data_dir.mkdir(parents=True)
    _create_database(data_dir / "argus.db")
    outside = tmp_path / "outside"
    called = []

    class MisplacedBackupController:
        def run(self, context, _timeout):
            bundle = harness.backup_database(
                Path(context["database_path"]),
                outside,
                APP_ID,
            )
            return {
                "ok": True,
                "application_id": APP_ID,
                "backup_path": str(bundle.path),
                "backup": bundle.evidence,
            }

    class Controller:
        def run(self, _context, _timeout):
            called.append(True)

    result = harness._run_public_controller(
        APP_ID,
        env={"LOCALAPPDATA": str(tmp_path / "local")},
        backup_controller=MisplacedBackupController(),
        controller=Controller(),
    )
    assert result["error"]["code"] == "backup_verification_failed"
    assert result["submission"] == "unknown"
    assert called == []


def test_windows_controller_source_has_kill_on_close_and_tree_termination():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "_KILL_ON_JOB_CLOSE = 0x00002000" in source
    assert "AssignProcessToJobObject" in source
    assert "TerminateJobObject" in source
    assert "gate_send_connection.send(True)" in source


def test_nonreturning_worker_terminates_exact_fake_job_tree(harness, monkeypatch):
    events = []

    class ResultReceive:
        def poll(self, timeout):
            events.append(("poll", timeout))
            return False

        def close(self):
            events.append("receive_close")

    class ResultSend:
        def close(self):
            events.append("result_send_close")

    class GateReceive:
        def close(self):
            events.append("gate_receive_close")

    class GateSend:
        def send(self, value):
            assert value is True
            events.append("gate_open")

        def close(self):
            events.append("gate_send_close")

    class Process:
        pid = 901
        sentinel = 902

        def __init__(self):
            self.alive = True

        def start(self):
            events.append("process_start")

        def join(self, timeout):
            events.append(("join", timeout))

        def is_alive(self):
            return self.alive

        def terminate(self):
            events.append("process_terminate")

    process = Process()

    class ProcessContext:
        def __init__(self):
            self.pipe_count = 0

        def Pipe(self, duplex):
            assert duplex is False
            self.pipe_count += 1
            if self.pipe_count == 1:
                return ResultReceive(), ResultSend()
            return GateReceive(), GateSend()

        def Process(self, **kwargs):
            assert kwargs["target"] is harness._worker_entry
            return process

    class Job:
        def assign(self, assigned):
            assert assigned is process
            events.append("job_assign")

        def terminate(self):
            events.append("job_terminate")
            process.alive = False
            return True

        def is_empty(self):
            events.append("job_empty")
            return True

        def close(self):
            events.append("job_close")
            return True

    monkeypatch.delenv(harness._WORKER_TOKEN_ENV, raising=False)
    controller = harness._WindowsJobController(
        platform_name="nt",
        process_context_factory=lambda method: ProcessContext()
        if method == "spawn"
        else None,
        job_factory=Job,
        clock=iter((10.0, 10.006)).__next__,
    )
    result = controller.run(
        {
            "application_id": APP_ID,
            "token": "one-time-token",
            "backup": {"filename": "verified.db"},
        },
        0.01,
    )
    assert result["error"]["code"] == "deadline_exceeded"
    assert result["submission"] == "unknown"
    assert result["cleanup"]["process_tree_terminated"] is True
    assert events == [
        "process_start",
        "result_send_close",
        "gate_receive_close",
        "job_assign",
        "gate_open",
        "gate_send_close",
        ("poll", pytest.approx(0.004)),
        "job_terminate",
        ("join", 5.0),
        "job_empty",
        "receive_close",
        "job_close",
    ]
    assert harness._WORKER_TOKEN_ENV not in __import__("os").environ


def test_job_assignment_failure_terminates_unassigned_child_immediately(harness):
    events = []

    class Connection:
        def close(self):
            events.append("connection_close")

    class Process:
        pid = 903
        sentinel = 904

        def __init__(self):
            self.alive = True

        def start(self):
            events.append("process_start")

        def is_alive(self):
            return self.alive

        def terminate(self):
            events.append("process_terminate")
            self.alive = False

        def join(self, timeout):
            events.append(("join", timeout))

    process = Process()

    class ProcessContext:
        def Pipe(self, duplex):
            assert duplex is False
            return Connection(), Connection()

        def Process(self, **_kwargs):
            return process

    class RejectingJob:
        def assign(self, assigned):
            assert assigned is process
            events.append("job_assign_failed")
            raise RuntimeError("assignment refused")

        def terminate(self):
            events.append("job_terminate")

        def close(self):
            events.append("job_close")
            return True

    controller = harness._WindowsJobController(
        platform_name="nt",
        process_context_factory=lambda _method: ProcessContext(),
        job_factory=RejectingJob,
    )
    result = controller.run(
        {"application_id": APP_ID, "token": "one-time-token", "backup": {}},
        1.0,
    )
    assert result["error"]["code"] == "controller_failed"
    assert result["cleanup"]["process_tree_terminated"] is True
    assert "process_terminate" in events
    assert "job_terminate" not in events
    assert events.index("process_terminate") < events.index(("join", 5.0))


@pytest.mark.skipif(os.name != "nt", reason="real Windows Job Object probe")
def test_real_windows_controller_invalid_context_probe_is_harmless(harness):
    result = harness._WindowsJobController().run(
        {"application_id": APP_ID, "token": "", "backup": {}},
        15.0,
    )
    assert result["error"]["code"] == "worker_parent_context_invalid"
    assert result["submission"] == "unknown"
    assert result["cleanup"]["attempted"] is False
    assert result["cleanup"]["worker_alive"] is False
    assert result["controller_lifecycle"] == {
        "job_assigned": True,
        "gate_opened": True,
        "result_received": True,
        "natural_exit": True,
        "job_handle_closed": True,
    }


def test_plan_insertion_does_not_duplicate_a_control_that_moves_position(harness):
    class Journey:
        step_index = 0

        def __init__(self):
            self.actions = ()

        def _build_plan(self, _fields, _evidence):
            return SimpleNamespace(actions=self.actions)

        def _fill_resolved_action(self, _scope, _action_value):
            return None

    first = _action("identity.first_name", selector="#control-a")
    second = _action("identity.last_name", selector="#control-b")
    inserted = _action("contact.email", selector="#control-x")
    capture = harness._FieldCapture()
    with harness._instrument_journey(Journey, capture):
        journey = Journey()
        journey.actions = (first, second)
        journey._build_plan([], {})
        journey.actions = (
            _action("identity.first_name", selector="#control-a"),
            inserted,
            _action("identity.last_name", selector="#control-b"),
        )
        journey._build_plan([], {})
    payload = capture.payload()
    assert [field["ordinal"] for field in payload["fields"]] == [
        "step-1-field-1",
        "step-1-field-2",
        "step-1-field-3",
    ]
    assert payload["resolved_action_count"] == 3
    assert len(payload["unfilled"]) == 3


def test_same_control_blocked_then_resolved_then_filled_updates_one_projection(harness):
    class Journey:
        step_index = 0

        def __init__(self):
            self.actions = ()

        def _build_plan(self, _fields, _evidence):
            return SimpleNamespace(actions=self.actions)

        def _fill_resolved_action(self, _scope, _action_value):
            return None

    capture = harness._FieldCapture()
    with harness._instrument_journey(Journey, capture):
        journey = Journey()
        journey.actions = (
            _action(
                "contact.email",
                "blocked",
                selector="#stable-control",
                source="missing",
            ),
        )
        journey._build_plan([], {})
        resolved = _action("contact.email", selector="#stable-control")
        journey.actions = (resolved,)
        journey._build_plan([], {})
        journey._fill_resolved_action(None, resolved)
    payload = capture.payload()
    assert payload["fields"] == [
        {
            "ordinal": "step-1-field-1",
            "required": True,
            "canonical_key": "contact.email",
            "control_category": "text",
            "status": "resolved",
            "source": "approved_profile",
        }
    ]
    assert payload["before_filled_count"] == 1
    assert payload["after_filled_count"] == 1
    assert payload["failed_fields"] == []
    assert payload["unfilled"] == []


def test_public_backup_phase_consumes_absolute_deadline_and_never_starts_worker(
    harness,
    tmp_path,
):
    data_dir = tmp_path / "local" / "ARGUS"
    data_dir.mkdir(parents=True)
    _create_database(data_dir / "argus.db")
    clock = iter((100.0, 100.0, 106.0)).__next__

    class BackupController:
        def run(self, context, timeout):
            assert timeout == 5.0
            bundle = harness._backup_database_to_path(
                Path(context["database_path"]),
                Path(context["backup_path"]),
                APP_ID,
            )
            return {
                "ok": True,
                "application_id": APP_ID,
                "backup_path": str(bundle.path),
                "backup": bundle.evidence,
            }

    class RuntimeController:
        calls = 0

        def run(self, _context, _timeout):
            self.calls += 1
            raise AssertionError("runtime worker started after the absolute deadline")

    runtime = RuntimeController()
    result = harness._run_public_controller(
        APP_ID,
        env={"LOCALAPPDATA": str(tmp_path / "local")},
        backup_controller=BackupController(),
        controller=runtime,
        deadline=5.0,
        clock=clock,
    )
    assert result["error"]["code"] == "deadline_exceeded"
    assert result["cleanup"]["worker_alive"] is False
    assert runtime.calls == 0


def test_timed_out_backup_phase_removes_only_its_exact_partial_files(harness, tmp_path):
    data_dir = tmp_path / "local" / "ARGUS"
    data_dir.mkdir(parents=True)
    _create_database(data_dir / "argus.db")
    observed = {}

    class BackupController:
        def run(self, context, _timeout):
            partial = Path(context["backup_partial_path"])
            final = Path(context["backup_path"])
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            final.write_bytes(b"unverified")
            observed.update(partial=partial, final=final)
            return harness._worker_failure(
                APP_ID,
                "deadline_exceeded",
                cleanup_attempted=False,
                cleanup_complete=None,
                worker_alive=False,
                process_tree_terminated=True,
            )

    class RuntimeController:
        def run(self, _context, _timeout):
            raise AssertionError("runtime worker started after backup timeout")

    result = harness._run_public_controller(
        APP_ID,
        env={"LOCALAPPDATA": str(tmp_path / "local")},
        backup_controller=BackupController(),
        controller=RuntimeController(),
        deadline=5.0,
    )
    assert result["error"]["code"] == "deadline_exceeded"
    assert not observed["partial"].exists()
    assert not observed["final"].exists()


def _fake_controller_context(harness, process, job, *, poll_result, received=None):
    class ResultReceive:
        def poll(self, _timeout):
            return poll_result

        def recv(self):
            return received

        def close(self):
            return None

    class Endpoint:
        def send(self, _value):
            return None

        def close(self):
            return None

    class ProcessContext:
        def __init__(self):
            self.count = 0

        def Pipe(self, duplex):
            assert duplex is False
            self.count += 1
            if self.count == 1:
                return ResultReceive(), Endpoint()
            return Endpoint(), Endpoint()

        def Process(self, **_kwargs):
            return process

    return harness._WindowsJobController(
        platform_name="nt",
        process_context_factory=lambda _method: ProcessContext(),
        job_factory=lambda: job,
    )


def test_failed_job_termination_never_claims_tree_terminated(harness):
    class Process:
        sentinel = 910
        alive = True

        def start(self):
            return None

        def join(self, timeout):
            assert timeout <= 5.0

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False

    class Job:
        def assign(self, _process):
            return None

        def terminate(self):
            raise harness.HarnessFailure("job_object_terminate_failed")

        def is_empty(self):
            return False

        def close(self):
            return True

    process = Process()
    result = _fake_controller_context(
        harness,
        process,
        Job(),
        poll_result=False,
    ).run({"application_id": APP_ID, "token": "token"}, 0.01)
    assert result["ok"] is False
    assert result["cleanup"]["process_tree_terminated"] is False
    assert result["cleanup"]["worker_alive"] is True
    assert result["controller_lifecycle"]["job_handle_closed"] is True


def test_failed_job_close_never_claims_handle_closed(harness):
    class Process:
        sentinel = 911

        def start(self):
            return None

        def join(self, timeout):
            assert timeout <= 5.0

        def is_alive(self):
            return False

        def close(self):
            return None

    class Job:
        def assign(self, _process):
            return None

        def terminate(self):
            return True

        def is_empty(self):
            return True

        def close(self):
            raise harness.HarnessFailure("job_object_close_failed")

    received = harness._worker_failure(
        APP_ID,
        "worker_parent_context_invalid",
        cleanup_attempted=False,
        worker_alive=False,
        process_tree_terminated=False,
    )
    result = _fake_controller_context(
        harness,
        Process(),
        Job(),
        poll_result=True,
        received=received,
    ).run({"application_id": APP_ID, "token": "token"}, 1.0)
    assert result["ok"] is False
    assert result["error"]["code"] == "controller_failed"
    assert result["controller_lifecycle"]["job_handle_closed"] is False


def test_job_wrapper_checks_failed_terminate_and_close_bool_results(harness):
    class Kernel:
        def TerminateJobObject(self, _handle, _code):
            return 0

        def CloseHandle(self, _handle):
            return 0

    job = harness._WindowsKillOnCloseJob.__new__(harness._WindowsKillOnCloseJob)
    job._kernel32 = Kernel()
    job._handle = 123
    with pytest.raises(harness.HarnessFailure, match="job_object_terminate_failed"):
        job.terminate()
    with pytest.raises(harness.HarnessFailure, match="job_object_close_failed"):
        job.close()
    assert job._handle == 123


def test_every_harness_sqlite_connection_is_explicitly_closed(harness, tmp_path, monkeypatch):
    database_path = tmp_path / "argus.db"
    _create_database(database_path)
    before = harness._snapshot_database(database_path)
    real_connect = sqlite3.connect
    opened = []

    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def backup(self, destination):
            target = getattr(destination, "connection", destination)
            return self.connection.backup(target)

        def close(self):
            self.connection.close()
            self.closed = True

    def tracked_connect(*args, **kwargs):
        proxy = ConnectionProxy(real_connect(*args, **kwargs))
        opened.append(proxy)
        return proxy

    sqlite_proxy = SimpleNamespace(
        connect=tracked_connect,
        Connection=sqlite3.Connection,
        DatabaseError=sqlite3.DatabaseError,
        IntegrityError=sqlite3.IntegrityError,
    )
    monkeypatch.setattr(harness, "sqlite3", sqlite_proxy)
    bundle = harness.backup_database(database_path, tmp_path / "backups", APP_ID)
    harness._snapshot_database(bundle.path)
    harness._application_gate(database_path, APP_ID)
    harness._verified_backup_evidence(bundle, data_dir=tmp_path, application_id=APP_ID)
    harness._semantic_run_and_audit_invariants(
        database_path,
        before,
        before,
        APP_ID,
        "missing-run",
        "NEEDS_USER",
    )
    assert opened
    assert all(connection.closed for connection in opened)


def test_identical_controls_use_per_plan_occurrence_and_rerender_dedupes(harness):
    owner = SimpleNamespace(step_index=0)
    capture = harness._FieldCapture()
    first_plan = SimpleNamespace(
        actions=(
            _action("contact.email", selector="#repeated-control"),
            _action("contact.email", selector="#repeated-control"),
        )
    )
    second_plan = SimpleNamespace(
        actions=(
            _action("contact.email", selector="#repeated-control"),
            _action("contact.email", selector="#repeated-control"),
        )
    )
    capture.plan(owner, first_plan)
    capture.plan(owner, second_plan)
    payload = capture.payload()
    assert [field["ordinal"] for field in payload["fields"]] == [
        "step-1-field-1",
        "step-1-field-2",
    ]


@pytest.mark.skipif(os.name != "nt", reason="real Windows Job Object probe")
def test_real_backup_worker_invalid_context_probe_is_harmless(harness):
    result = harness._WindowsJobController(
        target=harness._backup_worker_entry,
        token_environment_key=harness._BACKUP_TOKEN_ENV,
        process_name="argus-phase21-backup-probe",
    ).run(
        {"application_id": APP_ID, "token": ""},
        15.0,
    )
    assert result["error"]["code"] == "backup_parent_context_invalid"
    assert result["cleanup"]["attempted"] is False
    assert result["cleanup"]["worker_alive"] is False
    assert result["controller_lifecycle"] == {
        "job_assigned": True,
        "gate_opened": True,
        "result_received": True,
        "natural_exit": True,
        "job_handle_closed": True,
    }


@pytest.mark.skipif(os.name != "nt", reason="real Windows Job Object probe")
def test_real_windows_timeout_reaps_root_and_grandchild_job(harness, tmp_path):
    marker = tmp_path / "descendant.pid"
    result = harness._WindowsJobController(
        target=_harmless_nonreturning_tree_worker,
        process_name="argus-phase21-tree-probe",
    ).run(
        {
            "application_id": APP_ID,
            "token": "probe-token",
            "marker_path": str(marker),
        },
        1.0,
    )
    assert marker.is_file()
    assert int(marker.read_text(encoding="ascii")) > 0
    assert result["error"]["code"] == "deadline_exceeded"
    assert result["cleanup"]["worker_alive"] is False
    assert result["cleanup"]["process_tree_terminated"] is True
    assert result["controller_lifecycle"]["job_handle_closed"] is True
