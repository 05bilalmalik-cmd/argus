from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.services.review_digest import build_review_digest, send_review_digest


def setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return settings, db


_url_counter = {"n": 0}


def _seed(session, employer: str, state: str) -> Application:
    _url_counter["n"] += 1
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
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
    )
    session.add(application)
    session.flush()
    return application


def test_build_review_digest_buckets_real_states(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    with db.session_scope() as session:
        _seed(session, "Acme", ApplicationState.NEEDS_USER.value)
        _seed(session, "Acme", ApplicationState.NEEDS_OA.value)
        _seed(session, "Beta", ApplicationState.READY_TO_SUBMIT.value)
        _seed(session, "Gamma", ApplicationState.BLOCKED.value)
        _seed(session, "Delta", ApplicationState.FAILED_RETRYABLE.value)
        _seed(session, "Zeta", ApplicationState.QUEUED.value)  # ignored
        digest = build_review_digest(session)

    assert digest == {
        "ready_for_review": 3,
        "blocked": 1,
        "failed": 1,
    }


def test_send_review_digest_does_nothing_on_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}),
            db=db,
        )
    )

    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.review_digest.build_review_digest",
        lambda s: {"ready_for_review": 0, "blocked": 0, "failed": 0},
    )

    def fake_from_settings(_s):
        class _N:
            enabled = True
            _backend = type("B", (), {"send": lambda self, p: calls.append(p)})()
        return _N()
    monkeypatch.setattr("app.services.review_digest.NotificationService", type("NS", (), {"from_settings": staticmethod(fake_from_settings)})())

    result = send_review_digest(app)
    assert result is False
    assert calls == []


def test_send_review_digest_sends_one_summary_when_positive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings, db = setup(tmp_path)
    with db.session_scope() as session:
        _seed(session, "ExCo", ApplicationState.NEEDS_USER.value)

    app = SimpleNamespace(
        state=SimpleNamespace(settings=settings, db=db)
    )

    payloads: list[dict] = []

    class _RecBackend:
        def send(self, payload: dict) -> None:
            payloads.append(payload)

    class _FakeNotifier:
        enabled = True
        _backend = _RecBackend()

    def fake_from(_s):
        return _FakeNotifier()

    monkeypatch.setattr(
        "app.services.review_digest.NotificationService",
        type("NS", (), {"from_settings": staticmethod(fake_from)}),
    )

    result = send_review_digest(app)
    assert result is True
    assert len(payloads) == 1
    p = payloads[0]
    assert p["event"] == "review_digest"
    assert "ready_for_review" in str(p.get("message", ""))
    assert p["digest"]["ready_for_review"] >= 1
