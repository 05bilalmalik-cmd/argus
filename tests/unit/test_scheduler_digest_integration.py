"""Integration tests for review_digest call after autopilot in scheduler sweep."""

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.scouting import scheduler


def _make_app_with_mocks(tmp_path: Path, *, autopilot_enabled: bool = True):
    env = {"ARGUS_DATA_DIR": str(tmp_path)}
    if autopilot_enabled:
        env["ARGUS_AUTOMATION_MODE"] = "REVIEW_ONLY"
    settings = Settings.load(env)
    mock_session = object()
    cm = MagicMock()
    cm.__enter__.return_value = mock_session
    cm.__exit__.return_value = False

    mock_db = MagicMock()
    mock_db.session_scope.return_value = cm

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            db=mock_db,
            crypto=MagicMock(),
            navigator=MagicMock(),
        )
    )
    return app, settings


def test_send_review_digest_called_after_successful_autopilot(monkeypatch, tmp_path: Path) -> None:
    app, _ = _make_app_with_mocks(tmp_path)

    # prevent real fs/db work by stubbing the inner imports and scout
    monkeypatch.setattr("app.scouting.scheduler.trackr_folder", lambda d: tmp_path / "trackr_saves")
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    # fixture adaptation for the new explicit public-source dependency:
    # scheduled sweep now harvests public HTTP sources; stub to empty here
    # so this digest test never performs real network.
    monkeypatch.setattr(
        "app.scouting.sources.base.collect_all_report",
        lambda settings=None, sources=None, deadline_seconds=None, is_cancelled=None: ([], {
            "attempted": 0, "succeeded": 0, "failed": 0,
            "per_source": {}, "errors": {}, "total_raw": 0, "total": 0,
            "skipped": [], "truncated": None, "duplicate_sources": [], "collector_error": None}),
    )

    mock_scout = MagicMock()
    mock_scout.ingest.return_value = {"ingested": 0, "duplicates": 0, "errors": 0}
    mock_scout.run_autopilot.return_value = {"attempted": 0, "succeeded": 0}
    monkeypatch.setattr("app.scouting.service.ScoutService", lambda session, settings, crypto: mock_scout)

    digest_calls = []
    def fake_send(a):
        digest_calls.append(a)
        return True
    monkeypatch.setattr("app.services.review_digest.send_review_digest", fake_send)

    result = scheduler._run_sweep_locked(app)

    assert "autopilot" in result
    assert result["autopilot"] == {"attempted": 0, "succeeded": 0}
    assert len(digest_calls) == 1
    assert digest_calls[0] is app


def test_digest_exception_does_not_propagate_or_fail_sweep(monkeypatch, tmp_path: Path) -> None:
    app, _ = _make_app_with_mocks(tmp_path)

    monkeypatch.setattr("app.scouting.scheduler.trackr_folder", lambda d: tmp_path / "trackr_saves")
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    # fixture adaptation for the new explicit public-source dependency (see above).
    monkeypatch.setattr(
        "app.scouting.sources.base.collect_all_report",
        lambda settings=None, sources=None, deadline_seconds=None, is_cancelled=None: ([], {
            "attempted": 0, "succeeded": 0, "failed": 0,
            "per_source": {}, "errors": {}, "total_raw": 0, "total": 0,
            "skipped": [], "truncated": None, "duplicate_sources": [], "collector_error": None}),
    )

    mock_scout = MagicMock()
    mock_scout.ingest.return_value = {"ingested": 0}
    mock_scout.run_autopilot.return_value = {"ran": 1}
    monkeypatch.setattr("app.scouting.service.ScoutService", lambda s, st, c: mock_scout)

    def boom(a):
        raise RuntimeError("intentional digest failure for test")
    monkeypatch.setattr("app.services.review_digest.send_review_digest", boom)

    # must not raise, and must not set sweep error
    result = scheduler._run_sweep_locked(app)

    assert "autopilot" in result
    assert "error" not in result
    assert result["autopilot"] == {"ran": 1}
