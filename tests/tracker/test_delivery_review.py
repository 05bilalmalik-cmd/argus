from __future__ import annotations

import os
import smtplib
import sqlite3
import ssl
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.tracker import alerts as alerts_module
from app.tracker.alerts import (
    AlertConfig,
    AlertMessage,
    AlertService,
    DeliveryReceipt,
    DeliveryUncertainError,
    ProviderRefusedError,
    SmtpAlertSender,
)
from app.tracker.contracts import Listing, SourceResult
from app.tracker.pipeline import AutomationPipeline
from app.tracker.store import TrackerStore


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class FixedSender:
    def __init__(self, result):
        self.result = result
        self.messages: list[AlertMessage] = []
        self.lock = threading.Lock()

    def send(self, message: AlertMessage):
        with self.lock:
            self.messages.append(message)
        return self.result


def ready_config(**overrides):
    value = {
        "enabled": True,
        "transport": "injected",
        "from_address": "tracker@example.test",
        "to_address": "candidate@example.test",
        "retry_backoff_seconds": 0,
        "max_attempts": 3,
    }
    value.update(overrides)
    return value


def job(key: str = "role-1", **overrides) -> dict:
    value = {
        "id": None,
        "identity_key": key,
        "employer": "Example Bank",
        "title": "Summer Finance Internship 2028",
        "url": f"https://jobs.example.test/roles/{key}",
        "location": "London, UK",
        "programme": "summer",
        "stage": "not_applied",
        "availability": "open",
        "verified_at": "2026-09-11T08:00:00+00:00",
        "verification_error": "",
        "match_status": "potential",
        "uk_match": True,
        "deadline": None,
    }
    value.update(overrides)
    return value


def alert_message() -> AlertMessage:
    return AlertMessage(
        event_keys=("event-1",),
        roles=(),
        subject="fixture",
        body="fixture",
        message_id="<fixture@argus.invalid>",
        delivery_id="fixture-delivery",
        from_address="tracker@example.test",
        to_address="candidate@example.test",
    )


class RecordingSMTP:
    instances: list["RecordingSMTP"] = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = None
        self.login_calls: list[tuple[str, str]] = []
        self.send_calls = 0
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self, *args, **kwargs):
        self.context = kwargs.get("context", args[0] if args else None)
        return (220, b"ready")

    def login(self, user, password):
        self.login_calls.append((user, password))
        return (235, b"accepted")

    def send_message(self, email):
        self.send_calls += 1
        return {}

    def quit(self):
        return (221, b"bye")

    def close(self):
        return None


class QuitDisconnectSMTP(RecordingSMTP):
    def quit(self):
        raise smtplib.SMTPServerDisconnected("QUIT failed after DATA was accepted")


class DataDisconnectSMTP(RecordingSMTP):
    def send_message(self, email):
        raise smtplib.SMTPServerDisconnected("disconnect during DATA")


def smtp_config(**overrides) -> AlertConfig:
    value = {
        "enabled": True,
        "transport": "smtp",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_user": "fixture@example.test",
        "smtp_password": "fixture-password",
        "from_address": "tracker@example.test",
        "to_address": "candidate@example.test",
    }
    value.update(overrides)
    return AlertConfig.from_dict(value)


def test_smtp_sender_requires_a_certificate_validating_starttls_context(monkeypatch):
    RecordingSMTP.instances.clear()
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", RecordingSMTP)

    SmtpAlertSender(smtp_config()).send(alert_message())

    client = RecordingSMTP.instances[0]
    context = client.context
    assert (
        context is not None,
        context.verify_mode if context is not None else None,
        context.check_hostname if context is not None else False,
    ) == (True, ssl.CERT_REQUIRED, True)


def test_credential_probe_also_uses_certificate_validation_without_network(monkeypatch):
    RecordingSMTP.instances.clear()
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", RecordingSMTP)
    from app.tracker.mail_settings import check_credentials

    check_credentials(smtp_config())

    context = RecordingSMTP.instances[0].context
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_smtp_password_auth_is_not_allowed_without_tls(monkeypatch):
    RecordingSMTP.instances.clear()
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", RecordingSMTP)

    with pytest.raises(ProviderRefusedError, match="verified TLS"):
        SmtpAlertSender(smtp_config(smtp_tls=False)).send(alert_message())

    assert RecordingSMTP.instances == []


def test_alert_service_requires_an_explicit_positive_delivery_receipt(tmp_path: Path):
    specimens = [
        ("none", None),
        ("empty-mapping", {}),
        ("accepted-but-uncertain", DeliveryReceipt(accepted=True, uncertain=True)),
    ]
    observed = []
    for name, receipt in specimens:
        sender = FixedSender(receipt)
        service = AlertService(tmp_path / name, config=ready_config(), sender=sender)
        service.run([], now=NOW)
        result = service.run([job(name)], now=NOW)
        event = service.list_events()[0]
        observed.append((name, result["status"], result["sent"], event["status"]))

    assert observed == [
        (name, "uncertain", 0, "uncertain")
        for name, _receipt in specimens
    ]


def test_known_smtp_acceptance_survives_quit_disconnect_without_resend(
    tmp_path: Path, monkeypatch
):
    QuitDisconnectSMTP.instances.clear()
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", QuitDisconnectSMTP)
    config = smtp_config(retry_backoff_seconds=0)
    service = AlertService(tmp_path, config=config)

    service.run([], now=NOW)
    first = service.run([job("accepted")], now=NOW)
    second = service.run([job("accepted")], now=NOW)
    event = service.list_events()[0]

    assert (
        first["status"],
        first["sent"],
        second["sent"],
        len(QuitDisconnectSMTP.instances),
        event["status"],
    ) == ("sent", 1, 0, 1, "sent")


def test_disconnect_during_data_is_uncertain_not_a_known_refusal(monkeypatch):
    DataDisconnectSMTP.instances.clear()
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", DataDisconnectSMTP)

    with pytest.raises(DeliveryUncertainError):
        SmtpAlertSender(smtp_config()).send(alert_message())


def test_sendmail_timeout_is_uncertain_and_never_retried(tmp_path: Path, monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(alerts_module.subprocess, "run", timeout)
    service = AlertService(
        tmp_path,
        config=ready_config(
            transport="sendmail",
            sendmail_path="/fixture/sendmail",
            smtp_timeout_seconds=1,
        ),
    )

    service.run([], now=NOW)
    first = service.run([job("sendmail-timeout")], now=NOW)
    second = service.run([job("sendmail-timeout")], now=NOW)
    event = service.list_events()[0]

    assert (first["status"], first["uncertain"], second["sent"], len(calls), event["status"]) == (
        "uncertain",
        1,
        0,
        1,
        "uncertain",
    )


def test_restart_recovery_marks_an_abandoned_claim_uncertain_and_does_not_resend(
    tmp_path: Path,
):
    service = AlertService(tmp_path, config={"enabled": False})
    service.run([], now=NOW)
    service.run([job("abandoned")], now=NOW)
    event_key = service.list_events()[0]["event_key"]
    claimed_at = NOW.isoformat()
    with sqlite3.connect(service.outbox_path) as connection:
        connection.execute(
            """UPDATE alert_events SET status = 'sending', owner_pid = ?,
               claimed_at = ?, claimed_at_epoch = ? WHERE event_key = ?""",
            (os.getpid() + 1_000_000, claimed_at, NOW.timestamp(), event_key),
        )

    sender = FixedSender(DeliveryReceipt(accepted=True, delivery_id="should-not-send"))
    restarted = AlertService(tmp_path, config=ready_config(), sender=sender)
    assert restarted.list_events()[0]["status"] == "uncertain"
    result = restarted.run([job("abandoned")], now=NOW)

    assert result["sent"] == 0
    assert sender.messages == []
    assert restarted.list_events()[0]["status"] == "uncertain"


def test_pending_event_is_revalidated_before_send_when_role_closes(tmp_path: Path):
    service = AlertService(tmp_path, config={"enabled": False})
    service.run([], now=NOW)
    service.run([job("revalidate")], now=NOW)

    sender = FixedSender(DeliveryReceipt(accepted=True, delivery_id="not-used"))
    ready = AlertService(tmp_path, config=ready_config(), sender=sender)
    result = ready.run([job("revalidate", availability="closed")], now=NOW)
    event = ready.list_events()[0]

    assert result["sent"] == 0
    assert sender.messages == []
    assert (event["status"], event["retryable"], event["blocked"]) == (
        "failed",
        False,
        True,
    )


def test_pending_event_uses_current_role_snapshot_after_metadata_changes(tmp_path: Path):
    service = AlertService(tmp_path, config={"enabled": False})
    service.run([], now=NOW)
    service.run(
        [job("snapshot", title="Old Finance Internship", url="https://jobs.example.test/roles/old")],
        now=NOW,
    )

    sender = FixedSender(DeliveryReceipt(accepted=True, delivery_id="fixture"))
    ready = AlertService(tmp_path, config=ready_config(), sender=sender)
    ready.run(
        [job("snapshot", title="New Finance Internship", url="https://jobs.example.test/roles/new")],
        now=NOW,
    )

    assert sender.messages[0].roles[0]["title"] == "New Finance Internship"
    assert sender.messages[0].roles[0]["url"] == "https://jobs.example.test/roles/new"


def test_startup_baseline_does_not_flood_after_a_late_initial_verification_batch(
    tmp_path: Path,
):
    sender = FixedSender(DeliveryReceipt(accepted=True, delivery_id="fixture"))
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    first_batch = [
        job("already-verified"),
        job("waiting-for-verification", availability="unknown", verified_at=None),
    ]

    first = service.run(first_batch, now=NOW)
    second = service.run(
        [job("already-verified"), job("waiting-for-verification")],
        now=NOW,
    )

    assert first["baseline_suppressed"] == 1
    assert (second["created"], second["sent"], len(sender.messages)) == (0, 0, 0)


def test_simultaneous_alert_workers_claim_one_sqlite_batch_and_send_once(tmp_path: Path):
    root = tmp_path / "simultaneous"
    seed = AlertService(root, config=ready_config(), sender=FixedSender(None))
    seed.run([], now=NOW)
    first_sender = FixedSender(DeliveryReceipt(accepted=True, delivery_id="first"))
    second_sender = FixedSender(DeliveryReceipt(accepted=True, delivery_id="second"))
    first = AlertService(root, config=ready_config(), sender=first_sender)
    second = AlertService(root, config=ready_config(), sender=second_sender)
    barrier = threading.Barrier(2)
    results: list[dict | None] = [None, None]

    def invoke(index: int, service: AlertService):
        barrier.wait()
        results[index] = service.run([job("same-role")], now=NOW)

    threads = [
        threading.Thread(target=invoke, args=(0, first)),
        threading.Thread(target=invoke, args=(1, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(result is not None for result in results)
    assert sum(result["sent"] for result in results if result is not None) == 1
    assert len(first_sender.messages) + len(second_sender.messages) == 1
    events = first.list_events()
    assert len(events) == 1
    assert events[0]["status"] == "sent"


class PipelineIntelligence:
    def __init__(self, result):
        self.result = dict(result)
        self.seen = []

    def run(self, jobs):
        self.seen = list(jobs)
        return dict(self.result)

    def decorate(self, jobs):
        return [
            {
                **job,
                "availability": "open",
                "verified_at": NOW.isoformat(),
                "verification_error": "",
                "match_status": "potential",
                "uk_match": True,
            }
            for job in jobs
        ]

    def summary(self):
        return {"queued": self.result.get("queued", 0)}


class PipelineAlerts:
    def __init__(self, result, health):
        self.result = dict(result)
        self.health = dict(health)
        self.seen = []

    def run(self, jobs):
        self.seen = list(jobs)
        return dict(self.result)

    def summary(self):
        return dict(self.health)


def make_pipeline(tmp_path: Path, intelligence_result, alert_result, alert_health):
    store = TrackerStore(tmp_path / "tracker.sqlite3")
    intelligence = PipelineIntelligence(intelligence_result)
    alerts = PipelineAlerts(alert_result, alert_health)

    def collect():
        return [
            SourceResult(
                "fixture",
                "https://source.example.test/jobs",
                "ok",
                [
                    Listing(
                        "Example Bank",
                        "Summer Finance Internship 2028",
                        "https://source.example.test/jobs/1",
                        "London",
                        "summer",
                    )
                ],
                checked_at=NOW.isoformat(),
            )
        ]

    return AutomationPipeline(tmp_path, store, collect, intelligence, alerts)


def test_pipeline_degrades_for_historical_alert_failures_even_when_current_run_is_idle(
    tmp_path: Path,
):
    pipeline = make_pipeline(
        tmp_path,
        {"checked": 1, "queued": 0, "errors": 0},
        {"status": "idle", "sent": 0, "failed": 0, "uncertain": 0, "pending": 0},
        {
            "status": "ready",
            "config_status": "ready",
            "success": False,
            "failed": 1,
            "uncertain": 1,
            "pending": 0,
        },
    )

    pipeline.run()

    assert pipeline.snapshot()["stages"]["delivery"]["status"] == "partial"


def test_readyz_does_not_report_ready_for_historical_alert_failures(tmp_path: Path):
    intelligence = PipelineIntelligence({"checked": 1, "queued": 0, "errors": 0})
    alerts = PipelineAlerts(
        {"status": "idle", "sent": 0, "failed": 0, "uncertain": 0, "pending": 0},
        {
            "status": "ready",
            "config_status": "ready",
            "success": False,
            "failed": 1,
            "uncertain": 1,
            "pending": 0,
        },
    )

    def collect():
        return [
            SourceResult(
                "fixture",
                "https://source.example.test/jobs",
                "ok",
                [
                    Listing(
                        "Example Bank",
                        "Summer Finance Internship 2028",
                        "https://source.example.test/jobs/1",
                        "London",
                        "summer",
                    )
                ],
                checked_at=NOW.isoformat(),
            )
        ]

    from app.tracker.server import create_tracker_app

    app = create_tracker_app(
        tmp_path,
        collector=collect,
        auto_refresh=False,
        automated=True,
        intelligence=intelligence,
        alerts=alerts,
    )
    assert app.state.refresh.refresh_sync()
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["stages"]["delivery"] == "partial"


def test_pipeline_degrades_when_verification_queue_remains(tmp_path: Path):
    pipeline = make_pipeline(
        tmp_path,
        {"checked": 1, "queued": 3, "errors": 0},
        {"status": "idle", "sent": 0, "failed": 0, "uncertain": 0, "pending": 0},
        {"status": "ready", "config_status": "ready", "success": True},
    )

    pipeline.run()

    assert pipeline.snapshot()["stages"]["verification"]["status"] == "partial"


def test_pipeline_degrades_when_alert_events_remain_queued(tmp_path: Path):
    pipeline = make_pipeline(
        tmp_path,
        {"checked": 1, "queued": 0, "errors": 0},
        {"status": "sent", "sent": 1, "failed": 0, "uncertain": 0, "pending": 7},
        {
            "status": "ready",
            "config_status": "ready",
            "success": False,
            "failed": 0,
            "uncertain": 0,
            "pending": 7,
        },
    )

    pipeline.run()

    assert pipeline.snapshot()["stages"]["delivery"]["status"] == "partial"
