from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.automation.runner import AutomationRunner
from app.automation.types import (
    AutomationOutcome,
    RunMode,
    SessionSnapshot,
    SessionState,
)
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, CandidateProfile, Opportunity
from app.security.crypto import CryptoBox
from app.services.applications import ApplicationService


def _notifications():
    try:
        return importlib.import_module("app.services.notifications")
    except ModuleNotFoundError as exc:
        pytest.fail(f"notification service is not implemented: {exc}")


class RecordingBackend:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.payloads: list[dict[str, object]] = []

    def send(self, payload: dict[str, object]) -> None:
        if self.error is not None:
            raise self.error
        self.payloads.append(payload)


class ManualScheduler:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def __call__(self, _delay: float, callback):
        self.callbacks.append(callback)
        return SimpleNamespace(cancel=lambda: None)

    def fire(self) -> None:
        callback = self.callbacks.pop(0)
        callback()


def immediate_scheduler(_delay: float, callback):
    callback()
    return SimpleNamespace(cancel=lambda: None)


class RecordingHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object):
        self.calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None)


class RecordingSubprocessRunner:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object):
        captured = list(argv)
        self.calls.append((captured, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(captured, 0)


def _event(
    application_id: str = "app-1",
    *,
    state: str = "NEEDS_USER",
    reason: str = "captcha",
):
    module = _notifications()
    return module.HumanAttentionEvent(
        application_id=application_id,
        employer="Goldman Sachs",
        role="2027 Placement",
        state=state,
        reason=reason,
    )


def _service(
    backend: RecordingBackend,
    *,
    batch_window_seconds: float = 0,
    rate_limit_per_hour: int = 6,
    scheduler=immediate_scheduler,
    state_path: Path | None = None,
    clock=None,
):
    module = _notifications()
    return module.NotificationService(
        enabled=True,
        backend=backend,
        local_base_url="http://127.0.0.1:8787",
        batch_window_seconds=batch_window_seconds,
        rate_limit_per_hour=rate_limit_per_hour,
        scheduler=scheduler,
        state_path=state_path,
        clock=clock,
    )


def test_needs_user_event_sends_one_specific_one_line_notification() -> None:
    backend = RecordingBackend()
    notifier = _service(backend)

    assert notifier.notify(_event(reason="captcha")) is True

    assert len(backend.payloads) == 1
    assert backend.payloads[0]["message"] == (
        "ARGUS: Goldman Sachs — 2027 Placement. Captcha. -> "
        "http://127.0.0.1:8787/needs-you/app-1"
    )
    assert "\n" not in str(backend.payloads[0]["message"])


def test_reason_contract_rejects_free_text_and_pii() -> None:
    with pytest.raises(ValueError, match="reason code"):
        _event(reason="Email demo.candidate@example.test about this captcha")


def test_reason_contract_rejects_unknown_snake_case_values() -> None:
    with pytest.raises(ValueError, match="approved reason code"):
        _event(reason="candidate_name")


def test_cv_integrity_handoff_maps_to_the_specific_closed_reason() -> None:
    module = _notifications()

    assert module.select_human_attention_reason(
        state="NEEDS_USER",
        detail="Upload and approve a programme-compatible CV",
    ) == "required_cv_missing"


def test_notification_deep_link_resolves_to_the_exact_application(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    with TestClient(app) as client:
        with client.app.state.db.session_scope() as session:
            opportunity = Opportunity(
                employer="Goldman Sachs",
                role_title="2027 Placement",
                cycle="2027",
                url="https://example.test/programme",
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.NEEDS_USER.value,
            )
            session.add(application)
            session.flush()
            application_id = application.id

        response = client.get(
            f"/needs-you/{application_id}", follow_redirects=False
        )
        missing = client.get("/needs-you/not-a-real-application")

    assert response.status_code == 303
    assert response.headers["location"] == f"/applications/{application_id}"
    assert missing.status_code == 404


def test_same_state_and_reason_is_deduplicated_but_changed_reason_notifies() -> None:
    backend = RecordingBackend()
    notifier = _service(backend)

    assert notifier.notify(_event(reason="required_cv_missing")) is True
    assert notifier.notify(_event(reason="required_cv_missing")) is False
    assert notifier.notify(_event(reason="sensitive_demographic")) is True

    assert len(backend.payloads) == 2
    assert "Required CV missing" in str(backend.payloads[0]["message"])
    assert "Sensitive demographic" in str(backend.payloads[1]["message"])


def test_simultaneous_blocks_are_sent_as_one_digest() -> None:
    backend = RecordingBackend()
    scheduler = ManualScheduler()
    notifier = _service(backend, batch_window_seconds=30, scheduler=scheduler)

    for index, reason in enumerate(
        ("required_cv_missing", "sensitive_demographic", "assessment_handoff"),
        start=1,
    ):
        assert notifier.notify(_event(f"app-{index}", reason=reason)) is True

    assert backend.payloads == []
    assert len(scheduler.callbacks) == 1
    scheduler.fire()

    assert len(backend.payloads) == 1
    payload = backend.payloads[0]
    assert payload["count"] == 3
    assert len(payload["items"]) == 3
    assert "3 applications need you" in str(payload["message"])


def test_hourly_rate_limit_logs_and_drops_instead_of_queueing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = RecordingBackend()
    notifier = _service(backend, rate_limit_per_hour=1)

    with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
        assert notifier.notify(_event("app-1", reason="captcha")) is True
        assert notifier.notify(_event("app-2", reason="assessment_handoff")) is False

    assert len(backend.payloads) == 1
    assert "hourly rate limit reached; dropping notification" in caplog.text


def test_serialised_payload_excludes_every_candidate_profile_value() -> None:
    module = _notifications()
    profile = CandidateProfile(
        first_name="Demo",
        last_name="Demo",
        preferred_name="Demo",
        email="demo.candidate@example.test",
        phone="+44-7700-PII-123",
        address_line1="19 PII Example Street",
        city="PII-City",
        postcode="PII 1AA",
        country="PII-Country",
        linkedin_url="https://linkedin.example/pii-candidate",
        university="PII University",
        degree="PII Degree",
        current_study_year="PII Study Year",
        preferred_locations_json='["PII Location"]',
        work_authorisation_ciphertext="PII work authorisation value",
        sponsorship_required_ciphertext="PII sponsorship value",
    )
    opportunity = Opportunity(
        id="opportunity-42",
        employer="Goldman Sachs",
        role_title="2027 Placement",
        cycle="2027",
        url="https://example.test/job",
    )
    application = Application(
        id="application-42",
        opportunity_id=opportunity.id,
        opportunity=opportunity,
        state=ApplicationState.NEEDS_USER.value,
    )
    backend = RecordingBackend()
    notifier = _service(backend)

    notifier.notify(
        module.HumanAttentionEvent.from_application(
            application,
            reason="approved_legal_answer_missing",
        )
    )

    serialised = json.dumps(backend.payloads[0], sort_keys=True)
    personal_values = (
        profile.first_name,
        profile.last_name,
        profile.preferred_name,
        profile.email,
        profile.phone,
        profile.address_line1,
        profile.city,
        profile.postcode,
        profile.country,
        profile.linkedin_url,
        profile.university,
        profile.degree,
        profile.current_study_year,
        "PII Location",
        profile.work_authorisation_ciphertext,
        profile.sponsorship_required_ciphertext,
    )
    assert all(value not in serialised for value in personal_values)


def test_notifier_is_disabled_by_default_and_settings_require_explicit_opt_in(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    backend = RecordingBackend()
    notifier = module.NotificationService.from_settings(settings, backend=backend)

    assert settings.notifications_enabled is False
    assert settings.notify_batch_window_seconds == 30
    assert settings.notify_rate_limit_per_hour == 6
    assert settings.hermes_notify_target is None
    assert settings.hermes_bin == "hermes"
    assert notifier.notify(_event()) is False
    assert backend.payloads == []
    assert list(tmp_path.glob("notification_delivery_state*")) == []


def test_notification_settings_parse_all_explicit_backends(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NTFY_TOPIC": "argus-phone",
            "ARGUS_NTFY_SERVER": "https://notify.example.test/base/",
            "ARGUS_NOTIFY_WEBHOOK_URL": "https://hooks.example.test/argus",
            "ARGUS_HERMES_NOTIFY_TARGET": "telegram",
            "ARGUS_HERMES_BIN": r"C:\Tools\hermes-test.exe",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "12.5",
            "ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR": "4",
        }
    )

    assert settings.notifications_enabled is True
    assert settings.ntfy_topic == "argus-phone"
    assert settings.ntfy_server == "https://notify.example.test/base"
    assert settings.notify_webhook_url == "https://hooks.example.test/argus"
    assert settings.hermes_notify_target == "telegram"
    assert settings.hermes_bin == r"C:\Tools\hermes-test.exe"
    assert settings.notify_batch_window_seconds == 12.5
    assert settings.notify_rate_limit_per_hour == 4


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ARGUS_NOTIFY_BATCH_WINDOW_SECONDS", "-1"),
        ("ARGUS_NOTIFY_BATCH_WINDOW_SECONDS", "nan"),
        ("ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR", "0"),
        ("ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR", "1.5"),
    ],
)
def test_invalid_notification_limits_fail_closed(
    tmp_path: Path, name: str, value: str
) -> None:
    with pytest.raises(ValueError, match=name):
        Settings.load({"ARGUS_DATA_DIR": str(tmp_path), name: value})


@pytest.mark.parametrize(
    ("overrides", "expected_url", "body_key"),
    [
        (
            {"ARGUS_NTFY_TOPIC": "argus phone/phase12"},
            "https://ntfy.sh/argus%20phone%2Fphase12",
            "content",
        ),
        (
            {"ARGUS_NOTIFY_WEBHOOK_URL": "https://hooks.example.test/argus"},
            "https://hooks.example.test/argus",
            "json",
        ),
    ],
)
def test_http_backends_post_through_their_own_client(
    tmp_path: Path,
    overrides: dict[str, str],
    expected_url: str,
    body_key: str,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
            **overrides,
        }
    )
    client = RecordingHttpClient()
    notifier = module.NotificationService.from_settings(
        settings, http_client=client, scheduler=immediate_scheduler
    )

    notifier.notify(_event())

    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url == expected_url
    assert body_key in kwargs
    assert kwargs["timeout"] == 5


def test_unset_hermes_target_keeps_the_existing_stdout_backend(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    runner = RecordingSubprocessRunner()
    notifier = module.NotificationService.from_settings(
        settings,
        subprocess_runner=runner,
        scheduler=immediate_scheduler,
    )

    with caplog.at_level(logging.INFO, logger="app.services.notifications"):
        assert notifier.notify(_event()) is True

    assert isinstance(notifier._backend, module._StdoutBackend)
    assert runner.calls == []
    assert "ARGUS: Goldman Sachs" in caplog.text


@pytest.mark.parametrize("target", ["--exec", "email", ""])
def test_invalid_hermes_target_warns_and_constructs_no_hermes_backend(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    target: str,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_HERMES_NOTIFY_TARGET": target,
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    runner = RecordingSubprocessRunner()

    with caplog.at_level(logging.INFO, logger="app.services.notifications"):
        notifier = module.NotificationService.from_settings(
            settings,
            subprocess_runner=runner,
            scheduler=immediate_scheduler,
        )
        assert notifier.notify(_event()) is True

    assert isinstance(notifier._backend, module._StdoutBackend)
    assert runner.calls == []
    assert "invalid ARGUS_HERMES_NOTIFY_TARGET; hermes backend disabled" in caplog.text


def test_hermes_composes_with_http_backends_and_fanout_survives_one_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NTFY_TOPIC": "argus-phone",
            "ARGUS_NTFY_SERVER": "https://notify.example.test",
            "ARGUS_NOTIFY_WEBHOOK_URL": "https://hooks.example.test/argus",
            "ARGUS_HERMES_NOTIFY_TARGET": "telegram",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )

    class NtfyFailingClient(RecordingHttpClient):
        def post(self, url: str, **kwargs: object):
            self.calls.append((url, kwargs))
            if url == "https://notify.example.test/argus-phone":
                raise httpx.ConnectError("synthetic ntfy outage")
            return SimpleNamespace(raise_for_status=lambda: None)

    client = NtfyFailingClient()
    runner = RecordingSubprocessRunner()
    notifier = module.NotificationService.from_settings(
        settings,
        http_client=client,
        subprocess_runner=runner,
        scheduler=immediate_scheduler,
    )

    with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
        assert notifier.notify(_event()) is True

    assert [url for url, _kwargs in client.calls] == [
        "https://notify.example.test/argus-phone",
        "https://hooks.example.test/argus",
    ]
    assert len(runner.calls) == 1
    assert runner.calls[0][0][:4] == ["hermes", "send", "-t", "telegram"]
    assert "delivery failed; application run continues" in caplog.text


def test_plain_http_remote_notification_endpoint_is_rejected(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_WEBHOOK_URL": "http://hooks.example.test/argus",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    client = RecordingHttpClient()

    with caplog.at_level(logging.INFO, logger="app.services.notifications"):
        notifier = module.NotificationService.from_settings(
            settings, http_client=client, scheduler=immediate_scheduler
        )
        notifier.notify(_event())

    assert client.calls == []
    assert "webhook backend disabled" in caplog.text
    assert "ARGUS: Goldman Sachs" in caplog.text


def test_stdout_backend_is_a_one_line_local_demonstration(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    notifier = module.NotificationService.from_settings(
        settings, scheduler=immediate_scheduler
    )

    with caplog.at_level(logging.INFO, logger="app.services.notifications"):
        notifier.notify(_event())

    assert (
        "ARGUS: Goldman Sachs — 2027 Placement. Captcha. -> "
        "http://127.0.0.1:8787/needs-you/app-1"
    ) in caplog.text


def test_deduplication_is_shared_across_separately_constructed_runners(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    first_runner_notifier = module.NotificationService.from_settings(settings)
    second_runner_notifier = module.NotificationService.from_settings(settings)

    with caplog.at_level(logging.INFO, logger="app.services.notifications"):
        assert first_runner_notifier.notify(_event()) is True
        first_runner_notifier.flush()
        assert second_runner_notifier.notify(_event()) is False

    assert caplog.text.count("ARGUS: Goldman Sachs") == 1


def test_deduplication_survives_service_recreation(tmp_path: Path) -> None:
    state_path = tmp_path / "notification_delivery_state.json"
    first_backend = RecordingBackend()
    second_backend = RecordingBackend()

    first = _service(first_backend, state_path=state_path)
    assert first.notify(_event(reason="captcha")) is True

    recreated = _service(second_backend, state_path=state_path)
    assert recreated.notify(_event(reason="captcha")) is False
    assert second_backend.payloads == []
    with sqlite3.connect(state_path) as connection:
        state_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(notification_state)")
        }
    assert state_columns == {"application_id", "state", "last_reason", "updated_at"}


def test_durable_store_releases_its_sqlite_file_after_flush(tmp_path: Path) -> None:
    state_path = tmp_path / "notification_delivery_state.sqlite3"
    notifier = _service(RecordingBackend(), state_path=state_path)

    assert notifier.notify(_event("app-release", reason="captcha")) is True
    notifier.flush()
    state_path.unlink()

    assert state_path.exists() is False


def test_durable_state_merges_updates_from_independent_service_instances(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "notification_delivery_state.sqlite3"
    first = _service(RecordingBackend(), state_path=state_path)
    second = _service(RecordingBackend(), state_path=state_path)

    assert first.notify(_event("app-1", reason="captcha")) is True
    assert second.notify(_event("app-2", reason="assessment_handoff")) is True

    recreated_backend = RecordingBackend()
    recreated = _service(recreated_backend, state_path=state_path)
    assert recreated.notify(_event("app-1", reason="captcha")) is False
    assert recreated.notify(
        _event("app-2", reason="assessment_handoff")
    ) is False
    assert recreated_backend.payloads == []


def test_pending_digest_is_recovered_after_service_recreation(tmp_path: Path) -> None:
    state_path = tmp_path / "notification_delivery_state.sqlite3"
    original_scheduler = ManualScheduler()
    original = _service(
        RecordingBackend(),
        batch_window_seconds=30,
        scheduler=original_scheduler,
        state_path=state_path,
    )
    assert original.notify(_event("app-pending", reason="captcha")) is True

    recovered_backend = RecordingBackend()
    recovered = _service(
        recovered_backend,
        batch_window_seconds=30,
        scheduler=ManualScheduler(),
        state_path=state_path,
    )
    recovered.flush()

    assert len(recovered_backend.payloads) == 1
    assert "app-pending" in str(recovered_backend.payloads[0])


def test_stale_claim_reuses_one_idempotency_key_and_one_rate_reservation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "notification_delivery_state.sqlite3"
    scheduler = ManualScheduler()
    original = _service(
        RecordingBackend(),
        batch_window_seconds=30,
        rate_limit_per_hour=1,
        scheduler=scheduler,
        state_path=state_path,
    )
    assert original.notify(_event("app-crash", reason="captcha")) is True
    token, events, rate_limited = original._store.claim(1)
    assert token and len(events) == 1 and rate_limited is False

    # Simulate a process dying after its durable claim/reservation but before
    # backend completion. The lease is expired without changing its identity.
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE notification_outbox SET claimed_at = 0 WHERE claim_token = ?",
            (token,),
        )

    recovered_backend = RecordingBackend()
    recovered = _service(
        recovered_backend,
        batch_window_seconds=30,
        rate_limit_per_hour=1,
        scheduler=ManualScheduler(),
        state_path=state_path,
    )
    recovered.flush()

    assert len(recovered_backend.payloads) == 1
    assert recovered_backend.payloads[0]["idempotency_key"] == token
    with sqlite3.connect(state_path) as connection:
        deliveries = connection.execute(
            "SELECT delivery_key, status FROM notification_deliveries"
        ).fetchall()
        pending = connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
    assert deliveries == [(token, "completed")]
    assert pending == 0


def test_cross_process_capacity_collision_defers_without_recursive_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _notifications()
    state_path = tmp_path / "notification_delivery_state.sqlite3"
    scheduler = ManualScheduler()
    notifier = _service(
        RecordingBackend(),
        batch_window_seconds=30,
        rate_limit_per_hour=1,
        scheduler=scheduler,
        state_path=state_path,
    )
    assert notifier.notify(_event("app-pending", reason="captcha")) is True
    now = time.time()
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "INSERT INTO notification_deliveries "
            "(attempted_at, claim_token, delivery_key, reserved_at, status) "
            "VALUES (?, ?, ?, ?, 'completed')",
            (now, "other-process", "other-process", now),
        )
    retries: list[float] = []
    monkeypatch.setattr(
        module,
        "_thread_scheduler",
        lambda delay, _callback: (
            retries.append(delay) or SimpleNamespace(cancel=lambda: None)
        ),
    )

    scheduler.fire()

    assert len(retries) == 1
    assert retries[0] > 3500
    with sqlite3.connect(state_path) as connection:
        pending = connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
    assert pending == 1


def test_hourly_rate_limit_survives_service_recreation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_path = tmp_path / "notification_delivery_state.json"
    first = _service(
        RecordingBackend(), rate_limit_per_hour=1, state_path=state_path
    )
    assert first.notify(_event("app-1", reason="captcha")) is True

    recreated_backend = RecordingBackend()
    recreated = _service(
        recreated_backend, rate_limit_per_hour=1, state_path=state_path
    )
    with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
        assert recreated.notify(
            _event("app-2", reason="assessment_handoff")
        ) is False

    assert recreated_backend.payloads == []
    assert "hourly rate limit reached; dropping notification" in caplog.text


def test_shared_rate_limit_counts_an_inflight_cross_instance_delivery(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_path = tmp_path / "notification_delivery_state.sqlite3"
    started = threading.Event()
    release = threading.Event()

    class BlockingBackend:
        def send(self, _payload: dict[str, object]) -> None:
            started.set()
            release.wait(timeout=2)

    first = _service(
        BlockingBackend(),
        rate_limit_per_hour=1,
        scheduler=None,
        state_path=state_path,
    )
    assert first.notify(_event("app-inflight", reason="captcha")) is True
    assert started.wait(timeout=1)

    second_backend = RecordingBackend()
    second = _service(
        second_backend,
        rate_limit_per_hour=1,
        state_path=state_path,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
            assert second.notify(
                _event("app-dropped", reason="assessment_handoff")
            ) is False
    finally:
        release.set()
        first.flush()

    assert second_backend.payloads == []
    assert "hourly rate limit reached; dropping notification" in caplog.text


def test_default_delivery_never_waits_for_a_slow_backend() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingBackend:
        def send(self, _payload: dict[str, object]) -> None:
            started.set()
            release.wait(timeout=2)

    notifier = _service(BlockingBackend(), scheduler=None)
    before = time.perf_counter()
    assert notifier.notify(_event()) is True
    elapsed = time.perf_counter() - before
    try:
        assert elapsed < 0.25
        assert started.wait(timeout=1)
    finally:
        release.set()


def test_graceful_flush_waits_for_an_inflight_delivery() -> None:
    started = threading.Event()
    release = threading.Event()
    drain_complete = threading.Event()

    class BlockingBackend:
        def send(self, _payload: dict[str, object]) -> None:
            started.set()
            release.wait(timeout=2)

    notifier = _service(BlockingBackend(), scheduler=None)
    assert notifier.notify(_event()) is True
    assert started.wait(timeout=1)

    drain = threading.Thread(
        target=lambda: (notifier.flush(), drain_complete.set()), daemon=True
    )
    drain.start()
    try:
        assert drain_complete.wait(timeout=0.05) is False
    finally:
        release.set()
        drain.join(timeout=1)
    assert drain_complete.is_set()


def test_graceful_flush_tracks_a_worker_before_it_claims_events() -> None:
    backend = RecordingBackend()
    notifier = _service(backend, scheduler=ManualScheduler())
    claim_started = threading.Event()
    release_claim = threading.Event()
    drain_complete = threading.Event()
    calls = 0

    def racing_claim():
        nonlocal calls
        calls += 1
        if calls == 1:
            claim_started.set()
            release_claim.wait(timeout=2)
            return "stable-delivery", [_event("app-race")], False
        return "", [], False

    notifier._claim_events = racing_claim
    worker = threading.Thread(target=notifier._flush, daemon=True)
    worker.start()
    assert claim_started.wait(timeout=1)
    drain = threading.Thread(
        target=lambda: (notifier.flush(), drain_complete.set()), daemon=True
    )
    drain.start()
    try:
        assert drain_complete.wait(timeout=0.05) is False
    finally:
        release_claim.set()
        worker.join(timeout=1)
        drain.join(timeout=1)
    assert drain_complete.is_set()
    assert len(backend.payloads) == 1


def test_enabled_durable_store_failure_disables_delivery_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    backend = RecordingBackend()

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic unavailable store")

    monkeypatch.setattr(module, "_NotificationStore", unavailable)
    with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
        notifier = module.NotificationService(
            enabled=True,
            backend=backend,
            local_base_url="http://127.0.0.1:8787",
            batch_window_seconds=0,
            rate_limit_per_hour=6,
            state_path=tmp_path / "unavailable" / "state.sqlite3",
        )

    assert notifier.enabled is False
    assert notifier.notify(_event()) is False
    assert backend.payloads == []
    assert "notifications disabled" in caplog.text


def test_application_service_human_transition_notifies_without_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    backend = RecordingBackend()
    notifier = module.NotificationService.from_settings(
        settings, backend=backend, scheduler=immediate_scheduler
    )
    monkeypatch.setattr(
        module.NotificationService,
        "from_settings",
        classmethod(lambda _cls, _settings: notifier),
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            programme_group="summer",
            cycle="2027",
            url="https://example.test/programme",
            cv_required=True,
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id

    with database.session_scope() as session:
        prepared = ApplicationService(session, settings, crypto).prepare(application_id)
        assert prepared.ready is False

    assert len(backend.payloads) == 1
    assert "Required CV missing" in str(backend.payloads[0]["message"])


def test_nested_commit_waits_for_root_commit_and_nested_rollback_emits_nothing(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    notifier = _service(backend)
    module.install_application_notification_observer(database, notifier)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id

    session = database.SessionLocal()
    try:
        application = session.get(Application, application_id)
        assert application is not None
        nested = session.begin_nested()
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Upload and approve a programme-compatible CV"
        session.flush()
        nested.commit()
        assert backend.payloads == []
        session.commit()
    finally:
        session.close()
    assert len(backend.payloads) == 1

    with database.session_scope() as reset_session:
        application = reset_session.get(Application, application_id)
        assert application is not None
        application.state = ApplicationState.QUEUED.value
        application.next_action = ""
    backend.payloads.clear()
    session = database.SessionLocal()
    try:
        application = session.get(Application, application_id)
        assert application is not None
        nested = session.begin_nested()
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Captcha detected"
        session.flush()
        nested.rollback()
        session.commit()
    finally:
        session.close()
    assert backend.payloads == []


def test_nested_rollback_restores_same_application_root_transition(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    module.install_application_notification_observer(database, _service(backend))
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id

    session = database.SessionLocal()
    try:
        application = session.get(Application, application_id)
        assert application is not None
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Upload and approve a programme-compatible CV"
        session.flush()
        nested = session.begin_nested()
        application.next_action = "Captcha detected"
        session.flush()
        nested.rollback()
        session.commit()
    finally:
        session.close()

    assert len(backend.payloads) == 1
    assert "Required CV missing" in str(backend.payloads[0]["message"])


def test_outer_savepoint_rollback_removes_a_committed_inner_event(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    module.install_application_notification_observer(database, _service(backend))
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id

    session = database.SessionLocal()
    try:
        application = session.get(Application, application_id)
        assert application is not None
        outer = session.begin_nested()
        inner = session.begin_nested()
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Captcha detected"
        session.flush()
        inner.commit()
        outer.rollback()
        session.commit()
    finally:
        session.close()
    assert backend.payloads == []


def test_transient_human_state_resolved_before_root_commit_emits_nothing(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    module.install_application_notification_observer(database, _service(backend))
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id

    session = database.SessionLocal()
    try:
        application = session.get(Application, application_id)
        assert application is not None
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Captcha detected"
        session.flush()
        application.state = ApplicationState.READY_TO_SUBMIT.value
        application.next_action = "Review and submit"
        session.flush()
        session.commit()
    finally:
        session.close()
    assert backend.payloads == []


def test_nested_nonhuman_rollback_restores_parent_human_event(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    module.install_application_notification_observer(database, _service(backend))
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id

    session = database.SessionLocal()
    try:
        application = session.get(Application, application_id)
        assert application is not None
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Upload and approve a programme-compatible CV"
        session.flush()
        nested = session.begin_nested()
        application.state = ApplicationState.READY_TO_SUBMIT.value
        application.next_action = "Review and submit"
        session.flush()
        nested.rollback()
        session.commit()
    finally:
        session.close()
    assert len(backend.payloads) == 1
    assert "Required CV missing" in str(backend.payloads[0]["message"])


def test_json_only_reason_change_is_observed_for_existing_human_state(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.NEEDS_USER.value,
            next_action="Human review required",
        )
        session.add(application)
        session.flush()
        application_id = application.id
    backend = RecordingBackend()
    module.install_application_notification_observer(database, _service(backend))

    with database.session_scope() as session:
        application = session.get(Application, application_id)
        assert application is not None
        application.eligibility_json = json.dumps({"reason_codes": ["captcha"]})

    assert len(backend.payloads) == 1
    assert "Captcha" in str(backend.payloads[0]["message"])


def test_schema_reconciliation_notifies_a_raw_sql_human_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _notifications()
    disabled = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    disabled.ensure_directories()
    original = Database(disabled)
    original.create_schema()
    with original.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        session.add(
            Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.READY_TO_SUBMIT.value,
            )
        )
    original.engine.dispose()

    enabled = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    backend = RecordingBackend()
    notifier = _service(backend)
    monkeypatch.setattr(
        module.NotificationService,
        "from_settings",
        classmethod(lambda _cls, _settings: notifier),
    )
    reopened = Database(enabled)
    reopened.create_schema()

    assert len(backend.payloads) == 1
    assert "Application entry unresolved" in str(backend.payloads[0]["message"])


def test_standalone_navigator_human_boundary_notifies_without_runner(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            programme_group="summer",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id
    now = datetime.now(timezone.utc)
    snapshot = SessionSnapshot(
        session_id="navigator-session-1",
        application_id=application_id,
        mode=RunMode.REVIEW.value,
        state=SessionState.HUMAN_REQUIRED,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=5),
        reason="Captcha near demo.candidate@example.test",
        human_boundary={"kind": "captcha", "reason": "Captcha detected"},
    )
    navigator = SimpleNamespace(all_sessions=lambda: [snapshot])
    backend = RecordingBackend()
    notifier = module.NotificationService.from_settings(
        settings, backend=backend, scheduler=immediate_scheduler
    )

    monitor = module.NavigatorNotificationMonitor(navigator, database, notifier)
    monitor.scan_once()

    assert len(backend.payloads) == 1
    message = str(backend.payloads[0]["message"])
    assert "Captcha" in message
    assert "demo.candidate@example.test" not in message


def test_navigator_monitor_edge_cache_skips_an_unchanged_snapshot(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id
    now = datetime.now(timezone.utc)
    snapshot = SessionSnapshot(
        session_id="standalone-cache-session",
        application_id=application_id,
        mode=RunMode.REVIEW.value,
        state=SessionState.HUMAN_REQUIRED,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=5),
        reason="Captcha detected",
        human_boundary={"kind": "captcha", "reason": "Captcha detected"},
    )
    calls: list[object] = []
    notifier = SimpleNamespace(notify=lambda event: calls.append(event) or True)
    monitor = module.NavigatorNotificationMonitor(
        SimpleNamespace(all_sessions=lambda: [snapshot]), database, notifier
    )

    monitor.scan_once()
    monitor.scan_once()

    assert len(calls) == 1


def test_runner_owned_navigator_boundary_is_suppressed_until_committed_observer(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id
    now = datetime.now(timezone.utc)
    snapshot = SessionSnapshot(
        session_id="runner-session-1",
        application_id=application_id,
        mode=RunMode.REVIEW.value,
        state=SessionState.HUMAN_REQUIRED,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=5),
        reason="Captcha detected",
        summary=module.runner_owned_navigator_summary({"provider": "greenhouse"}),
        human_boundary={"kind": "captcha", "reason": "Captcha detected"},
    )
    navigator = SimpleNamespace(all_sessions=lambda: [snapshot])
    backend = RecordingBackend()
    notifier = _service(backend)
    module.install_application_notification_observer(database, notifier)
    monitor = module.NavigatorNotificationMonitor(navigator, database, notifier)

    # This is the shared-runner race window: Navigator has published its
    # boundary, while the application transaction is still PACKAGE_PREPARED.
    monitor.scan_once()
    assert backend.payloads == []
    with database.session_scope() as session:
        application = session.get(Application, application_id)
        assert application is not None
        application.state = ApplicationState.NEEDS_USER.value
        application.next_action = "Captcha detected"
    monitor.scan_once()

    assert len(backend.payloads) == 1
    assert "Captcha" in str(backend.payloads[0]["message"])


def test_enabled_app_lifecycle_starts_and_stops_notification_monitor(
    tmp_path: Path,
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        thread = client.app.state.notification_monitor._thread
        assert thread is not None and thread.is_alive()

    assert thread.is_alive() is False


def test_fail_closed_notifier_does_not_enable_navigator_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
        }
    )
    disabled = SimpleNamespace(enabled=False, notify=lambda _event: False, flush=lambda: None)
    monkeypatch.setattr(
        module.NotificationService,
        "from_settings",
        classmethod(lambda _cls, _settings: disabled),
    )

    app = create_app(settings)

    assert app.state.notifier.enabled is False
    assert app.state.notification_monitor._enabled is False


def _runner_setup(tmp_path: Path, settings: Settings):
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Goldman Sachs",
            role_title="2027 Placement",
            programme_group="unmapped-programme",
            cycle="2027",
            url="https://example.test/programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id
    return database, crypto, application_id


def test_runner_human_block_transition_calls_notifier_once_with_real_reason(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    backend = RecordingBackend()
    notifier = module.NotificationService.from_settings(
        settings, backend=backend, scheduler=immediate_scheduler
    )
    runner = AutomationRunner(database, settings, crypto, notifier=notifier)

    outcome = runner.run(application_id, RunMode.REVIEW)

    assert outcome.state == ApplicationState.NEEDS_USER.value
    assert len(backend.payloads) == 1
    assert "Programme framing required" in str(backend.payloads[0]["message"])


def test_runner_human_block_transition_invokes_hermes_with_one_message_argument(
    tmp_path: Path,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_HERMES_NOTIFY_TARGET": "telegram",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    subprocess_runner = RecordingSubprocessRunner()
    notifier = module.NotificationService.from_settings(
        settings,
        subprocess_runner=subprocess_runner,
        scheduler=immediate_scheduler,
    )

    outcome = AutomationRunner(database, settings, crypto, notifier=notifier).run(
        application_id, RunMode.REVIEW
    )

    assert outcome.state == ApplicationState.NEEDS_USER.value
    assert len(subprocess_runner.calls) == 1
    argv, kwargs = subprocess_runner.calls[0]
    expected_message = (
        "ARGUS: Goldman Sachs — 2027 Placement. Programme framing required. -> "
        f"http://127.0.0.1:8787/needs-you/{application_id}"
    )
    assert argv == ["hermes", "send", "-t", "telegram", expected_message]
    assert argv[-1] == expected_message
    assert kwargs == {
        "check": True,
        "shell": False,
        "stderr": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "timeout": 5,
    }


def test_hermes_argv_excludes_every_fully_populated_candidate_profile_value(
    tmp_path: Path,
) -> None:
    module = _notifications()
    profile = CandidateProfile(
        id=91573,
        first_name="Demo",
        last_name="Candidate",
        preferred_name="Demo",
        email="pii-argv@example.test",
        phone="+44-7700-ARGV-992",
        address_line1="81 PII Argv Crescent",
        city="PII-ARGV-CITY",
        postcode="ARGV 9ZZ",
        country="PII-ARGV-COUNTRY",
        linkedin_url="https://linkedin.example/pii-argv-profile",
        university="PII Argv University",
        degree="PII Argv Degree",
        graduation_year=2099,
        current_study_year="PII Argv Study Year",
        preferred_locations_json='["PII-ARGV-NORTH", "PII-ARGV-SOUTH"]',
        work_authorisation_ciphertext="PII argv work authorisation ciphertext",
        sponsorship_required_ciphertext="PII argv sponsorship ciphertext",
        work_authorisation_approved=True,
        updated_at=datetime(2035, 7, 8, tzinfo=timezone.utc),
    )
    opportunity = Opportunity(
        id="opportunity-hermes-pii",
        employer="Goldman Sachs",
        role_title="2027 Placement",
        cycle="2027",
        url="https://example.test/job",
    )
    application = Application(
        id="application-hermes-pii",
        opportunity_id=opportunity.id,
        opportunity=opportunity,
        state=ApplicationState.NEEDS_USER.value,
    )
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_HERMES_NOTIFY_TARGET": "telegram",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    subprocess_runner = RecordingSubprocessRunner()
    notifier = module.NotificationService.from_settings(
        settings,
        subprocess_runner=subprocess_runner,
        scheduler=immediate_scheduler,
    )

    assert notifier.notify(
        module.HumanAttentionEvent.from_application(
            application,
            reason="approved_legal_answer_missing",
        )
    ) is True

    assert len(subprocess_runner.calls) == 1
    captured_argv = json.dumps(subprocess_runner.calls[0][0], sort_keys=True)
    personal_values = (
        str(profile.id),
        profile.first_name,
        profile.last_name,
        profile.preferred_name,
        profile.email,
        profile.phone,
        profile.address_line1,
        profile.city,
        profile.postcode,
        profile.country,
        profile.linkedin_url,
        profile.university,
        profile.degree,
        str(profile.graduation_year),
        profile.current_study_year,
        profile.preferred_locations_json,
        "PII-ARGV-NORTH",
        "PII-ARGV-SOUTH",
        profile.work_authorisation_ciphertext,
        profile.sponsorship_required_ciphertext,
    )
    assert all(value not in captured_argv for value in personal_values)


def test_runner_dispatches_only_after_human_state_is_committed(tmp_path: Path) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    observed_states: list[str] = []

    class CommitObservingBackend:
        def send(self, _payload: dict[str, object]) -> None:
            with database.session_scope() as independent_session:
                persisted = independent_session.get(Application, application_id)
                assert persisted is not None
                observed_states.append(persisted.state)

    notifier = module.NotificationService.from_settings(
        settings,
        backend=CommitObservingBackend(),
        scheduler=immediate_scheduler,
    )
    outcome = AutomationRunner(
        database, settings, crypto, notifier=notifier
    ).run(application_id, RunMode.REVIEW)

    assert outcome.state == ApplicationState.NEEDS_USER.value
    assert observed_states == [ApplicationState.NEEDS_USER.value]


def test_notification_event_construction_failure_never_fails_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    runner = AutomationRunner(
        database,
        settings,
        crypto,
        notifier=module.NotificationService.from_settings(
            settings,
            backend=RecordingBackend(),
            scheduler=immediate_scheduler,
        ),
    )

    def invalid_event(*_args):
        raise ValueError("synthetic malformed notification event")

    monkeypatch.setattr(runner, "_human_attention_event", invalid_event)
    with caplog.at_level(logging.WARNING, logger="app.automation.runner"):
        outcome = runner.run(application_id, RunMode.REVIEW)

    assert outcome.state == ApplicationState.NEEDS_USER.value
    with database.session_scope() as session:
        persisted = session.get(Application, application_id)
        assert persisted is not None
        assert persisted.state == ApplicationState.NEEDS_USER.value
    assert "notification event construction failed" in caplog.text


@pytest.mark.parametrize(
    ("state", "blocked_reasons", "boundary", "expected_reason"),
    [
        (
            ApplicationState.NEEDS_OA.value,
            ("assessment_handoff",),
            {},
            "Assessment handoff",
        ),
        (
            ApplicationState.NEEDS_USER.value,
            ("human_boundary",),
            {"kind": "captcha", "reason": "Captcha near demo.candidate@example.test"},
            "Captcha",
        ),
    ],
)
def test_runner_maps_human_stops_to_controlled_non_pii_reason_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    blocked_reasons: tuple[str, ...],
    boundary: dict[str, str],
    expected_reason: str,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    backend = RecordingBackend()
    notifier = module.NotificationService.from_settings(
        settings, backend=backend, scheduler=immediate_scheduler
    )
    runner = AutomationRunner(database, settings, crypto, notifier=notifier)

    def human_stop(_session, application, *_args):
        application.state = state
        return AutomationOutcome(
            state=state,
            risk_level=3,
            adapter="test",
            blocked_reasons=blocked_reasons,
            human_boundary=boundary,
        )

    monkeypatch.setattr(runner, "_run_claimed", human_stop)
    runner.run(application_id, RunMode.REVIEW)

    assert len(backend.payloads) == 1
    message = str(backend.payloads[0]["message"])
    assert expected_reason in message
    assert "demo.candidate@example.test" not in message


def test_backend_timeout_never_fails_the_application_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    backend = RecordingBackend(httpx.TimeoutException("phone push timed out"))
    notifier = module.NotificationService.from_settings(
        settings, backend=backend, scheduler=immediate_scheduler
    )
    runner = AutomationRunner(database, settings, crypto, notifier=notifier)

    with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
        outcome = runner.run(application_id, RunMode.REVIEW)

    assert outcome.state == ApplicationState.NEEDS_USER.value
    assert "delivery failed; application run continues" in caplog.text


@pytest.mark.parametrize(
    ("error", "exception_name", "exit_fragment", "private_detail"),
    [
        (
            subprocess.CalledProcessError(
                23,
                ["hermes", "send", "-t", "telegram", "NONZERO-PRIVATE-DETAIL"],
            ),
            "CalledProcessError",
            "exit status 23",
            "NONZERO-PRIVATE-DETAIL",
        ),
        (
            subprocess.TimeoutExpired(
                ["hermes", "send", "-t", "telegram", "TIMEOUT-PRIVATE-DETAIL"],
                timeout=5,
            ),
            "TimeoutExpired",
            "",
            "TIMEOUT-PRIVATE-DETAIL",
        ),
        (
            FileNotFoundError("MISSING-BINARY-PRIVATE-DETAIL"),
            "FileNotFoundError",
            "",
            "MISSING-BINARY-PRIVATE-DETAIL",
        ),
    ],
)
def test_hermes_subprocess_failure_never_fails_the_application_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    exception_name: str,
    exit_fragment: str,
    private_detail: str,
) -> None:
    module = _notifications()
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_NOTIFICATIONS": "true",
            "ARGUS_HERMES_NOTIFY_TARGET": "telegram",
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS": "0",
        }
    )
    database, crypto, application_id = _runner_setup(tmp_path, settings)
    subprocess_runner = RecordingSubprocessRunner(error)
    notifier = module.NotificationService.from_settings(
        settings,
        subprocess_runner=subprocess_runner,
        scheduler=immediate_scheduler,
    )

    with caplog.at_level(logging.WARNING, logger="app.services.notifications"):
        outcome = AutomationRunner(database, settings, crypto, notifier=notifier).run(
            application_id, RunMode.REVIEW
        )

    assert outcome.state == ApplicationState.NEEDS_USER.value
    with database.session_scope() as session:
        persisted = session.get(Application, application_id)
        assert persisted is not None
        assert persisted.state == ApplicationState.NEEDS_USER.value
    assert len(subprocess_runner.calls) == 1
    assert "delivery failed; application run continues" in caplog.text
    assert exception_name in caplog.text
    if exit_fragment:
        assert exit_fragment in caplog.text
    assert private_detail not in caplog.text
    assert subprocess_runner.calls[0][0][-1] not in caplog.text


# ---------------------------------------------------------------------------
# Discovery notifications (new eligible opportunities)
# ---------------------------------------------------------------------------


def test_discovery_notify_sends_one_digest_for_eligible_new_opportunity(
    tmp_path: Path,
) -> None:
    """A newly inserted eligible opportunity queues exactly one discovery event."""
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    from app.db import Database
    from app.models import Opportunity
    from app.services.opportunities import OpportunityService

    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = OpportunityService(session)
        opportunity = service.add(
            Opportunity(
                employer="Test Bank",
                role_title="2027 Summer Analyst",
                cycle="2027",
                url="https://apply.testbank.example/intern",
            ),
            actor="test",
        )
        assert opportunity.is_open_for_applications
        assert not opportunity.is_archived
        pending = session.info.get(module._DISCOVERY_EVENTS_KEY, [])
        assert len(pending) == 1
        assert pending[0].employer == "Test Bank"
        assert pending[0].role_title == "2027 Summer Analyst"


def test_discovery_notify_skipped_for_archived_opportunity(
    tmp_path: Path,
) -> None:
    """Re-adding an already-archived opportunity queues no discovery event."""
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    from datetime import datetime, timezone
    from app.db import Database
    from app.models import Opportunity, OpportunityArchive
    from app.services.opportunities import OpportunityService

    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opp = Opportunity(
            employer="Archive Bank",
            role_title="2027 Placement",
            cycle="2027",
            url="https://archive.example/job",
        )
        session.add(opp)
        session.flush()
        session.add(
            OpportunityArchive(
                opportunity_id=opp.id,
                archived_at=datetime.now(timezone.utc),
                archived_reason="test_reason",
            )
        )
        session.commit()

    with database.session_scope() as session:
        service = OpportunityService(session)
        result = service.add(
            Opportunity(
                employer="Archive Bank",
                role_title="2027 Placement",
                cycle="2027",
                url="https://archive.example/job",
            ),
            actor="test",
        )
        assert result.is_archived
        pending = session.info.get(module._DISCOVERY_EVENTS_KEY, [])
        assert len(pending) == 0


def test_discovery_notify_skipped_for_closed_window_opportunity(
    tmp_path: Path,
) -> None:
    """An opportunity with CLOSED application window queues no discovery event."""
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    from app.db import Database
    from app.models import Opportunity
    from app.services.opportunities import OpportunityService

    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = OpportunityService(session)
        opportunity = Opportunity(
            employer="Closed Bank",
            role_title="2027 Spring Week",
            cycle="2027",
            url="https://closed.example/job",
            application_window_status="CLOSED",
        )
        saved = service.add(opportunity, actor="test")
        assert not saved.is_open_for_applications
        pending = session.info.get(module._DISCOVERY_EVENTS_KEY, [])
        assert len(pending) == 0


def test_discovery_payload_contains_no_pii_fields() -> None:
    """The discovery payload dict has no PII-bearing keys."""
    module = _notifications()
    from datetime import date

    event = module.OpportunityDiscoveredEvent(
        employer="Goldman Sachs",
        role_title="Summer Analyst",
        deadline="2027-01-15",
        application_url="https://apply.gs.example/summer",
    )
    backend = RecordingBackend()
    notifier = _service(backend)
    notifier.notify_discovery([event])

    assert len(backend.payloads) == 1
    payload_text = str(backend.payloads[0])
    pii_indicators = ("first_name", "last_name", "email", "phone", "address",
                      "cv", "resume", "cover_letter", "password", "candidate_name")
    for indicator in pii_indicators:
        assert indicator not in payload_text, f"PII indicator '{indicator}' found in payload"


def test_discovery_digest_batches_multiple_opportunities() -> None:
    """Multiple eligible opportunities in one session produce one digest."""
    module = _notifications()

    events = [
        module.OpportunityDiscoveredEvent(
            employer=f"Bank {i}", role_title=f"Role {i}",
            deadline=None, application_url=f"https://bank{i}.example/job",
        )
        for i in range(1, 4)
    ]
    backend = RecordingBackend()
    notifier = _service(backend)
    notifier.notify_discovery(events)

    assert len(backend.payloads) == 1
    payload = backend.payloads[0]
    assert payload["count"] == 3
    assert payload["event"] == "opportunity_discovered"
    assert "ARGUS discovered 3 new roles" in str(payload["message"])
    assert len(payload["items"]) == 3


def test_discovery_ntfy_title_overrides_default() -> None:
    """Discovery payload supplies a custom ntfy title via the 'title' key."""
    module = _notifications()

    event = module.OpportunityDiscoveredEvent(
        employer="Title Test",
        role_title="Role",
        deadline=None,
        application_url="https://title.example/job",
    )
    backend = RecordingBackend()
    notifier = _service(backend)
    notifier.notify_discovery([event])

    assert len(backend.payloads) == 1
    assert backend.payloads[0].get("title") == "ARGUS discovered new roles"


def test_discovery_disabled_when_notifier_off() -> None:
    """notify_discovery returns False when the notifier is disabled."""
    module = _notifications()

    event = module.OpportunityDiscoveredEvent(
        employer="Disabled Test", role_title="Role",
        deadline=None, application_url=None,
    )
    notifier = module.NotificationService(
        enabled=False,
        backend=RecordingBackend(),
        local_base_url="http://127.0.0.1:8787",
        batch_window_seconds=0,
        rate_limit_per_hour=6,
        scheduler=immediate_scheduler,
    )
    assert notifier.notify_discovery([event]) is False

def _deadline_application(session, *, employer="Deadline Bank", state="NEEDS_USER", due_in_hours=20):
    from datetime import datetime, timezone
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        cycle="2027",
        url=f"https://example.test/{employer.casefold().replace(' ', '-')}",
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=state,
        next_action="Complete online assessment",
        next_action_deadline=datetime.now(timezone.utc) + timedelta(hours=due_in_hours),
    )
    session.add(application)
    session.flush()
    return application


def test_deadline_reminder_fires_once_then_dedups(tmp_path: Path) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    notifier = _service(backend)
    with database.session_scope() as session:
        _deadline_application(session, due_in_hours=20)
        assert module.queue_deadline_reminders(session, notifier) == 1
        assert module.queue_deadline_reminders(session, notifier) == 0
    assert len(backend.payloads) == 1
    message = str(backend.payloads[0]["message"])
    assert "Deadline approaching" in message
    assert "/needs-you/" in message


def test_deadline_reminder_skips_far_and_nonblocking(tmp_path: Path) -> None:
    module = _notifications()
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    database.create_schema()
    backend = RecordingBackend()
    notifier = _service(backend)
    with database.session_scope() as session:
        _deadline_application(session, employer="Far Bank", due_in_hours=24 * 30)
        _deadline_application(session, employer="Oa Bank", state="OA_PENDING", due_in_hours=20)
        _deadline_application(session, employer="Queued Bank", state="QUEUED", due_in_hours=20)
        assert module.queue_deadline_reminders(session, notifier) == 0
    assert backend.payloads == []


def test_deadline_reminder_disabled_notifier_sends_nothing() -> None:
    from types import SimpleNamespace

    module = _notifications()
    assert module.queue_deadline_reminders(None, SimpleNamespace(enabled=False)) == 0
