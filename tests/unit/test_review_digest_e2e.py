"""End-to-end review-digest behaviour with a fully mocked notification sink.

Covers gaps NOT asserted by tests/unit/test_review_digest.py or
tests/unit/test_scheduler_digest_integration.py:

- realistic mixed-state digest delivered as ONE payload with exact counts
- payload leaks no secrets / PII (digest is counts-only)
- empty database -> no notification at all (no empty spam, real empty DB)
- backend.send exception is swallowed (returns False, never raises)
- digest performs zero database writes (row state unchanged)
- nothing-configured environment silently returns False (documents current state)

All outbound delivery is faked. No real email/webhook/ntfy/hermes is used.
Tmp databases only; the production DB is never touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.services.review_digest import build_review_digest, send_review_digest


def _setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return settings, db


_url_counter = {"n": 0}


def _seed(session, employer: str, state: str, **overrides) -> Application:
    _url_counter["n"] += 1
    opportunity = Opportunity(
        employer=employer,
        role_title=overrides.pop("role_title", "Summer Analyst"),
        programme_group="summer",
        cycle="2027",
        url=(
            f"https://boards.example.com/"
            f"{employer.casefold().replace(' ', '-')}/{_url_counter['n']}"
        ),
        source="test",
        cv_required=False,
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=state,
        priority=50,
        next_action=overrides.pop("next_action", ""),
        eligibility_json=overrides.pop("eligibility_json", "{}"),
    )
    for key, value in overrides.items():
        setattr(application, key, value)
    session.add(application)
    session.flush()
    return application


def _seed_mixed(session) -> None:
    for i in range(3):
        _seed(session, f"NeedsUserCo{i}", ApplicationState.NEEDS_USER.value)
    for i in range(2):
        _seed(session, f"NeedsOACo{i}", ApplicationState.NEEDS_OA.value)
    _seed(session, "ReadyCo", ApplicationState.READY_TO_SUBMIT.value)
    for i in range(2):
        _seed(session, f"BlockedCo{i}", ApplicationState.BLOCKED.value)
    _seed(session, "FailedCo", ApplicationState.FAILED_RETRYABLE.value)
    # Ignored states must not leak into any bucket.
    _seed(session, "QueuedCo", ApplicationState.QUEUED.value)
    _seed(session, "QueuedCo2", ApplicationState.QUEUED.value)


def _app_with_fake_notifier(settings, db, monkeypatch, backend):
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, db=db))

    class _FakeNotifier:
        enabled = True
        _backend = backend

    def fake_from(_s):
        return _FakeNotifier()

    monkeypatch.setattr(
        "app.services.review_digest.NotificationService",
        type("NS", (), {"from_settings": staticmethod(fake_from)}),
    )
    return app


def test_e2e_mixed_states_single_payload_exact_counts(monkeypatch, tmp_path: Path) -> None:
    settings, db = _setup(tmp_path)
    with db.session_scope() as session:
        _seed_mixed(session)

    payloads: list[dict] = []

    class _RecBackend:
        def send(self, payload: dict) -> None:
            payloads.append(payload)

    app = _app_with_fake_notifier(settings, db, monkeypatch, _RecBackend())
    result = send_review_digest(app)

    assert result is True
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["event"] == "review_digest"
    assert payload["digest"] == {
        "ready_for_review": 6,  # 3 NEEDS_USER + 2 NEEDS_OA + 1 READY_TO_SUBMIT
        "blocked": 2,
        "failed": 1,
    }
    assert payload["count"] == 9
    message = str(payload["message"])
    assert "6 ready_for_review" in message
    assert "2 blocked" in message
    assert "1 failed" in message


def test_e2e_payload_leaks_no_secrets_or_pii(monkeypatch, tmp_path: Path) -> None:
    settings, db = _setup(tmp_path)
    secret_token = "sk-live-SECRET-XYZ-123"  # gitleaks:allow -- deliberately fake API token for redaction test
    secret_pw = "hunter2-pw-SECRET"  # gitleaks:allow -- deliberately fake password for redaction test
    employer_name = "VeryUniqueEmployerNameZZZ"
    contact_email = "candidate.secret99@example.com"
    with db.session_scope() as session:
        _seed(
            session,
            employer_name,
            ApplicationState.NEEDS_USER.value,
            next_action=f"call {contact_email} api_key={secret_token} password={secret_pw}",
            eligibility_json='{"reason_codes": ["captcha"], "token": "tok-secret-ABC"}',
        )

    payloads: list[dict] = []

    class _RecBackend:
        def send(self, payload: dict) -> None:
            payloads.append(payload)

    app = _app_with_fake_notifier(settings, db, monkeypatch, _RecBackend())
    assert send_review_digest(app) is True
    assert len(payloads) == 1
    payload = payloads[0]
    blob = " ".join(
        str(payload.get(key, "")) for key in ("message", "event", "count")
    ) + " " + str(payload.get("digest", ""))
    for needle in (
        secret_token,
        secret_pw,
        "tok-secret-ABC",
        employer_name,
        contact_email,
        "api_key",
        "password",
    ):
        assert needle not in blob, f"leaked {needle!r} into digest payload"
    # Digest payload is counts-only: no per-application items, no employer/role/url.
    assert set(payload.keys()) == {
        "event",
        "idempotency_key",
        "count",
        "message",
        "digest",
    }
    assert "items" not in payload


def test_e2e_empty_database_sends_nothing(monkeypatch, tmp_path: Path) -> None:
    """Real empty DB (no mocked build): enabled notifier still sends nothing."""
    settings, db = _setup(tmp_path)
    with db.session_scope() as session:
        digest = build_review_digest(session)
    assert digest == {"ready_for_review": 0, "blocked": 0, "failed": 0}

    calls: list[dict] = []

    class _RecBackend:
        def send(self, payload: dict) -> None:
            calls.append(payload)

    app = _app_with_fake_notifier(settings, db, monkeypatch, _RecBackend())
    assert send_review_digest(app) is False
    assert calls == []


def test_e2e_backend_exception_is_swallowed(monkeypatch, tmp_path: Path) -> None:
    settings, db = _setup(tmp_path)
    with db.session_scope() as session:
        _seed(session, "ExCo", ApplicationState.NEEDS_USER.value)

    class _BoomBackend:
        def send(self, payload: dict) -> None:
            raise RuntimeError("fake channel outage")

    app = _app_with_fake_notifier(settings, db, monkeypatch, _BoomBackend())
    # Must not propagate; digest is never load-bearing.
    assert send_review_digest(app) is False


def test_e2e_digest_performs_no_database_writes(monkeypatch, tmp_path: Path) -> None:
    settings, db = _setup(tmp_path)
    with db.session_scope() as session:
        _seed_mixed(session)

    def _snapshot():
        with db.session_scope() as session:
            rows = session.query(Application).order_by(Application.id).all()
            return (
                [(r.id, r.state, str(r.updated_at)) for r in rows],
                build_review_digest(session),
            )

    before_rows, before_digest = _snapshot()

    payloads: list[dict] = []

    class _RecBackend:
        def send(self, payload: dict) -> None:
            payloads.append(payload)

    app = _app_with_fake_notifier(settings, db, monkeypatch, _RecBackend())
    assert send_review_digest(app) is True
    assert len(payloads) == 1

    after_rows, after_digest = _snapshot()
    assert after_digest == before_digest
    assert after_rows == before_rows


def test_e2e_nothing_configured_silently_returns_false(tmp_path: Path) -> None:
    """Default tmp Settings (notifications disabled, no topic/webhook) -> False.

    Uses the REAL NotificationService.from_settings (no mock, stdout fallback
    only) to prove the silent early-return when nothing is configured.
    """
    settings, db = _setup(tmp_path)
    assert settings.notifications_enabled is False
    with db.session_scope() as session:
        _seed(session, "ExCo", ApplicationState.NEEDS_USER.value)

    app = SimpleNamespace(state=SimpleNamespace(settings=settings, db=db))
    assert send_review_digest(app) is False
