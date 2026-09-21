from __future__ import annotations

import multiprocessing
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.tracker.alerts import (
    AlertService,
    DeliveryReceipt,
    DeliveryUncertainError,
    ProviderRefusedError,
)


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class FakeSender:
    def __init__(self, *, failures=None):
        self.failures = list(failures or [])
        self.messages = []
        self.lock = threading.Lock()

    def send(self, message):
        with self.lock:
            self.messages.append(message)
            failure = self.failures.pop(0) if self.failures else None
        if failure:
            raise failure
        return {"accepted": True, "delivery_id": f"provider-{len(self.messages)}"}


def job(
    key="ats:greenhouse:acme:1001",
    *,
    availability="open",
    verified_at="2026-09-11T08:00:00+00:00",
    match_status="potential",
    uk_match=True,
    stage="not_applied",
    deadline="2026-09-18",
    url="https://boards.greenhouse.io/acme/jobs/1001",
    title="Finance Summer Internship",
):
    return {
        "id": None,
        "identity_key": key,
        "employer": "Acme Capital",
        "title": title,
        "url": url,
        "location": "London, UK",
        "programme": "summer",
        "stage": stage,
        "availability": availability,
        "verified_at": verified_at,
        "match_status": match_status,
        "uk_match": uk_match,
        "deadline": deadline,
        "deadline_text": deadline or "",
    }


def ready_config(**overrides):
    config = {
        "enabled": True,
        "transport": "injected",
        "from_address": "tracker@example.test",
        "to_address": "candidate@example.test",
        "retry_backoff_seconds": 0,
        "max_attempts": 3,
    }
    config.update(overrides)
    return config


def _crash_process_worker(root: str):
    class CrashSender:
        def send(self, message):
            raise SystemExit("simulated process crash after DATA")

    service = AlertService(Path(root), config=ready_config(), sender=CrashSender())
    service.run([], now=NOW)
    try:
        service.run([job("process-restart", deadline=None)], now=NOW)
    except SystemExit:
        pass


def test_disabled_delivery_is_blocked_and_does_not_claim_or_report_success(tmp_path: Path):
    sender = FakeSender()
    service = AlertService(tmp_path, config={"enabled": False}, sender=sender)

    assert service.run([job()], now=NOW)["status"] == "blocked"
    result = service.run([job("ats:greenhouse:acme:1002")], now=NOW)

    assert result["status"] == "blocked"
    assert result["config_status"] == "disabled"
    assert result["sent"] == 0
    assert not sender.messages
    assert service.summary()["blocked"] is True
    assert service.summary()["pending"] == 2
    assert service.summary()["success"] is False


def test_unverified_post_startup_role_is_not_permanently_suppressed_and_new_role_is_once(
    tmp_path: Path,
):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([], now=NOW)  # Subsequent discoveries are not the initial inventory.
    unverified = job("ats:greenhouse:acme:1001", availability="unknown", verified_at=None)

    baseline = service.run([unverified], now=NOW)
    assert baseline["baseline_suppressed"] == 0
    assert baseline["created"] == 0

    first = service.run([job()], now=NOW)
    second = service.run([job()], now=NOW)

    assert first["created"] == 2
    assert first["sent"] == 1
    assert second["created"] == 0
    assert second["sent"] == 0
    assert len(sender.messages) == 1
    events = service.list_events()
    assert len(events) == 2
    new_event = next(event for event in events if event["kind"] == "new")
    assert new_event["event_key"].startswith("new:")
    assert new_event["status"] == "sent"
    assert new_event["message_id"] == sender.messages[0].message_id
    assert new_event["message_id"] == service.list_events()[0 if events[0]["kind"] == "new" else 1]["message_id"]
    assert new_event["inbox_arrival_proven"] is False


def test_initial_verified_corpus_is_suppressed_but_explicit_bootstrap_is_bounded(
    tmp_path: Path,
):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(bootstrap_enabled=False), sender=sender)
    corpus = [job(f"ats:greenhouse:acme:{number}") for number in range(25)]

    result = service.run(corpus, now=NOW)
    assert result["baseline_suppressed"] == 25
    assert result["created"] == 0
    assert not sender.messages

    blocked = service.bootstrap_digest(corpus, now=NOW)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "bootstrap_disabled"
    assert not sender.messages

    enabled = AlertService(
        tmp_path / "explicit-bootstrap",
        config=ready_config(bootstrap_enabled=True),
        sender=sender,
    )
    result = enabled.bootstrap_digest(corpus, now=NOW)
    assert result["sent"] == 1
    assert result["roles"] == 10
    assert len(sender.messages[-1].roles) == 10


def test_reminders_are_exactly_seven_or_two_days_and_have_daily_dedup(tmp_path: Path):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([], now=NOW)  # establish the baseline without suppressing this role
    due = job("ats:greenhouse:acme:7", deadline="2026-09-18")

    first = service.run([due], now=NOW)
    again = service.run([due], now=NOW)
    not_a_window = service.run([due], now=datetime(2026, 9, 12, tzinfo=timezone.utc))

    assert first["created"] == 2  # the new-role and seven-day events
    assert again["created"] == 0
    assert not_a_window["created"] == 0
    assert len(service.list_events()) == 2

    two_days = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    due_on_two_day_window = {**due, "verified_at": "2026-09-16T08:00:00+00:00"}
    result = service.run([due_on_two_day_window], now=two_days)
    repeat = service.run([due_on_two_day_window], now=two_days)
    assert result["created"] == 1
    assert repeat["created"] == 0
    assert len(service.list_events()) == 3

    for invalid in ("2026-09-10", "soon", None):
        invalid_job = job(f"ats:greenhouse:acme:{invalid!s}", deadline=invalid)
        service.run([invalid_job], now=NOW)
    assert not any(
        event["kind"] == "reminder" and event["role_key"].endswith("invalid")
        for event in service.list_events()
    )


def test_known_refusal_retries_with_same_message_id_and_sanitized_error(tmp_path: Path):
    sender = FakeSender(failures=[ProviderRefusedError("SMTP password=top-secret 451")])
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([], now=NOW)

    failed = service.run([job()], now=NOW)
    assert failed["failed"] == 1
    assert failed["sent"] == 0
    first_message_id = sender.messages[0].message_id
    event = service.list_events()[0]
    assert event["status"] == "failed"
    assert "top-secret" not in event["last_error"]
    assert "password" not in event["last_error"].casefold()

    retried = service.run([job()], now=NOW)
    assert retried["sent"] == 1
    assert sender.messages[-1].message_id == first_message_id
    assert service.list_events()[0]["status"] == "sent"


def test_uncertain_delivery_and_crash_recovery_are_never_automatically_resent(tmp_path: Path):
    uncertain_sender = FakeSender(failures=[DeliveryUncertainError("disconnect after DATA")])
    service = AlertService(tmp_path / "uncertain", config=ready_config(), sender=uncertain_sender)
    service.run([], now=NOW)
    service.run([job()], now=NOW)
    assert service.list_events()[0]["status"] == "uncertain"
    service.run([job()], now=NOW)
    assert len(uncertain_sender.messages) == 1

    class CrashSender:
        def send(self, message):
            raise SystemExit("simulated process crash after DATA")

    crash_root = tmp_path / "crash"
    crashing = AlertService(crash_root, config=ready_config(), sender=CrashSender())
    crashing.run([], now=NOW)
    with pytest.raises(SystemExit):
        crashing.run([job()], now=NOW)

    restarted = AlertService(crash_root, config=ready_config(), sender=FakeSender())
    assert restarted.recover_incomplete(force=True) == 2
    assert restarted.list_events()[0]["status"] == "uncertain"
    result = restarted.run([job()], now=NOW)
    assert result["sent"] == 0
    assert not restarted.sender.messages


def test_actual_process_restart_recovers_sending_as_uncertain(tmp_path: Path):
    root = tmp_path / "process-restart"
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_process_worker, args=(str(root),))
    process.start()
    process.join(15)
    assert process.exitcode == 0

    restarted_sender = FakeSender()
    restarted = AlertService(root, config=ready_config(), sender=restarted_sender)
    events = restarted.list_events()
    assert len(events) == 1
    assert events[0]["status"] == "uncertain"
    result = restarted.run([job("process-restart", deadline=None)], now=NOW)
    assert result["sent"] == 0
    assert not restarted_sender.messages


def test_false_delivery_receipt_is_failed_not_success(tmp_path: Path):
    class DecliningSender:
        def send(self, message):
            return DeliveryReceipt(accepted=False, error="provider refused before DATA")

    service = AlertService(tmp_path, config=ready_config(), sender=DecliningSender())
    service.run([], now=NOW)
    result = service.run([job("declined", deadline=None)], now=NOW)
    assert result["status"] == "retry_scheduled"
    assert result["sent"] == 0
    assert service.list_events()[0]["status"] == "failed"


def test_dual_instances_claim_one_sqlite_batch_and_send_one_digest(tmp_path: Path):
    first_sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=first_sender)
    service.run([], now=NOW)
    current = job()

    started = threading.Event()
    release = threading.Event()

    class BlockingSender(FakeSender):
        def send(self, message):
            started.set()
            assert release.wait(5)
            return super().send(message)

    blocking_sender = BlockingSender()
    first = AlertService(tmp_path, config=ready_config(), sender=blocking_sender)
    second_sender = FakeSender()
    second = AlertService(tmp_path, config=ready_config(), sender=second_sender)
    results = []

    thread = threading.Thread(target=lambda: results.append(first.run([current], now=NOW)))
    thread.start()
    assert started.wait(5)
    second_result = second.run([current], now=NOW)
    release.set()
    thread.join(5)

    assert len(blocking_sender.messages) == 1
    assert not second_sender.messages
    assert second_result["sent"] == 0
    assert results and results[0]["sent"] == 1
    assert len(second.list_events()) == 2


def test_digest_is_one_call_and_no_more_than_twenty_roles_per_run(tmp_path: Path):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([], now=NOW)
    roles = [job(f"ats:greenhouse:acme:{number}") for number in range(25)]

    first = service.run(roles, now=NOW)
    assert first["sent"] == 1
    assert first["roles"] == 20
    assert len(sender.messages) == 1
    assert len(sender.messages[0].roles) == 20
    assert service.summary()["pending"] == 10

    second = service.run(roles, now=NOW)
    assert second["sent"] == 1
    assert len(sender.messages) == 2
    assert len(sender.messages[1].roles) == 5


def test_eligibility_is_fail_closed_for_expired_closed_stale_foreign_applied_and_excluded_roles(
    tmp_path: Path,
):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([], now=NOW)
    cases = [
        {"availability": "closed"},
        {"availability": "unknown"},
        {"verified_at": "2026-09-09T08:00:00+00:00"},
        {"uk_match": False},
        {"stage": "applied"},
        {"match_status": "excluded"},
        {"deadline": "2026-09-10"},
        {"title": "Software Engineering Internship", "match_status": "excluded"},
    ]
    for number, changes in enumerate(cases):
        candidate = job(f"role-{number}", deadline=None)
        candidate.update(changes)
        result = service.run([candidate], now=NOW)
        assert result["created"] == 0
        assert result["sent"] == 0
    assert not sender.messages
    assert service.summary()["pending"] == 0


def test_known_ats_identity_deduplicates_url_aliases_but_not_fuzzy_same_title_roles(
    tmp_path: Path,
):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([], now=NOW)
    baseline = job(
        "ignored-local-id",
        availability="unknown",
        verified_at=None,
        deadline=None,
        url="https://boards.greenhouse.io/acme/jobs/1001",
    )
    baseline["identity_key"] = None
    service.run([baseline], now=NOW)
    alias = {**baseline, "id": 2, "availability": "open", "verified_at": "2026-09-11T08:00:00+00:00",
             "url": "https://job-boards.greenhouse.io/acme/jobs/1001?gh_jid=1001"}
    assert service.run([alias], now=NOW)["created"] == 1
    unrelated = {**alias, "id": 3, "url": "https://boards.greenhouse.io/acme/jobs/1002"}
    assert service.run([unrelated], now=NOW)["created"] == 1
    new_events = [event for event in service.list_events() if event["kind"] == "new"]
    assert len(new_events) == 2
    assert len({event["role_key"] for event in new_events}) == 2


def test_changed_deadline_is_a_distinct_deduplicated_event(tmp_path: Path):
    sender = FakeSender()
    service = AlertService(tmp_path, config=ready_config(), sender=sender)
    service.run([job("role-deadline", deadline="2026-09-20")], now=NOW)
    changed = job("role-deadline", deadline="2026-09-22")
    first = service.run([changed], now=NOW)
    repeat = service.run([changed], now=NOW)
    assert first["created"] == 1
    assert repeat["created"] == 0
    assert [event["kind"] for event in service.list_events()] == ["deadline_change"]


def test_pending_event_is_revalidated_before_send_after_role_closes(tmp_path: Path):
    root = tmp_path / "revalidate"
    disabled = AlertService(root, config={"enabled": False})
    disabled.run([], now=NOW)
    disabled.run([job("role-revalidate", deadline=None)], now=NOW)
    sender = FakeSender()
    ready = AlertService(root, config=ready_config(), sender=sender)
    closed = job("role-revalidate", deadline=None, availability="closed")
    result = ready.run([closed], now=NOW)
    assert result["sent"] == 0
    assert not sender.messages
    event = ready.list_events()[0]
    assert event["status"] == "failed"
    assert event["blocked"] is True


def test_env_factory_uses_dedicated_names_without_persisting_secret(tmp_path: Path):
    from app.tracker.alerts import alert_config_from_env

    config = alert_config_from_env(
        {
            "ARGUS_TRACKER_ALERTS_ENABLED": "true",
            "ARGUS_TRACKER_TRANSPORT": "smtp",
            "ARGUS_TRACKER_SMTP_HOST": "smtp.example.test",
            "ARGUS_TRACKER_SMTP_PORT": "587",
            "ARGUS_TRACKER_SMTP_USER": "user",
            "ARGUS_TRACKER_SMTP_PASSWORD": "secret-value",
            "ARGUS_TRACKER_FROM": "from@example.test",
            "ARGUS_TRACKER_TO": "to@example.test",
        }
    )
    assert config.enabled is True
    assert config.smtp_host == "smtp.example.test"
    assert config.smtp_port == 587
    assert config.from_address == "from@example.test"
    service = AlertService(tmp_path, config={"enabled": False})
    assert "secret-value" not in service.summary()["last_error"]
