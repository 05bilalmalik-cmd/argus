"""Scheduled-discovery campaign: periodic sweep uses existing public HTTP
sources with truthful per-source reporting, Trackr as optional supplement.

No real network: all public sources are injected callables. No browser.
No submission confirmation: REVIEW_ONLY fixtures assert review-only stops.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.db import Database
from app.models import Opportunity
from app.scouting import scheduler
from app.scouting.sources.base import collect_all, collect_all_report
from app.scouting.trackr import ScrapedOpportunity


def _row(url: str, role: str = "Summer Analyst", employer: str = "Campaign Bank",
         source: str = "greenhouse:campaign") -> ScrapedOpportunity:
    return ScrapedOpportunity(employer=employer, role_title=role, url=url, source=source)


def _review_only_settings(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path),
                              "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY"})
    settings.ensure_directories()
    return settings


def _mock_app(tmp_path: Path, settings):
    from app.security.crypto import CryptoBox
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    (tmp_path / "trackr_saves").mkdir(parents=True, exist_ok=True)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, db=db,
                                                crypto=crypto,
                                                navigator=MagicMock()))
    return app


# --- collect_all_report: truthful bounded reporting, same merge as collect_all


def test_collect_all_report_matches_collect_all_items_and_reports_per_source():
    good = _row("https://boards.greenhouse.io/campaign/jobs/1", source="greenhouse:campaign")
    other = _row("https://jobs.lever.co/campaign/2", role="Spring Week",
                 source="lever:campaign")
    noise = ScrapedOpportunity(employer="BigCo", role_title="Managing Director",
                               url="https://bigco.example/jobs/99", source="lever:bigco")

    def broken():
        raise RuntimeError("boom")

    sources = [("broken", broken),
               ("good", lambda: [good]),
               ("other", lambda: [other]),
               ("noise", lambda: [noise])]
    items, report = collect_all_report(sources=sources)
    merged = collect_all(sources=sources)
    assert [m.url for m in items] == [m.url for m in merged]
    assert report["attempted"] == 4
    assert report["succeeded"] == 3
    assert report["failed"] == 1
    assert report["per_source"] == {"broken": 0, "good": 1, "other": 1, "noise": 1}
    assert set(report["errors"]) == {"broken"}
    # Whitelist redaction: type name only, never the arbitrary message.
    assert report["errors"]["broken"] == "RuntimeError"
    # noise row fetched but filtered from final items (not early-careers)
    assert all(r.role_title != "Managing Director" for r in items)


def test_collect_all_report_all_failed_is_not_empty_success():
    def broken():
        raise RuntimeError("down")

    items, report = collect_all_report(sources=[("a", broken), ("b", broken)])
    assert items == []
    assert report["succeeded"] == 0
    assert report["failed"] == 2
    assert len(report["errors"]) == 2


def test_collect_all_report_none_return_tolerated():
    items, report = collect_all_report(sources=[("nothing", lambda: None)])
    assert items == []
    assert report["succeeded"] == 1
    assert report["failed"] == 0


# --- scheduled sweep uses injected public sources (parity with manual sweep)


def test_scheduled_sweep_ingests_injected_public_sources(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.services.review_digest.send_review_digest", lambda a: True)

    injected = [("greenhouse:campaign",
                 lambda: [_row("https://boards.greenhouse.io/campaign/jobs/9")])]
    result = scheduler._run_sweep_locked(app, public_sources=injected)

    assert result["public_sources"]["succeeded"] == 1
    assert result["parsed"] == 1
    assert result["per_source"].get("greenhouse") == 1
    assert result["discovery"] == "ok"
    assert result["ingest"]["imported"] == 1
    with app.state.db.session_scope() as session:
        assert session.query(Opportunity).count() == 1


def test_scheduled_sweep_mixed_error_empty_report_is_partial(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.services.review_digest.send_review_digest", lambda a: True)

    def broken():
        raise RuntimeError("board down")

    injected = [("ok", lambda: [_row("https://boards.greenhouse.io/campaign/jobs/3")]),
                ("bad", broken),
                ("empty", lambda: [])]
    result = scheduler._run_sweep_locked(app, public_sources=injected)

    assert result["ingest"]["imported"] == 1
    assert result["discovery"] == "partial"
    assert result["public_sources"]["failed"] == 1
    assert set(result["public_sources"]["errors"]) == {"bad"}


def test_scheduled_sweep_all_failed_is_not_empty_success(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.services.review_digest.send_review_digest", lambda a: True)

    def broken():
        raise RuntimeError("down")

    result = scheduler._run_sweep_locked(
        app, public_sources=[("a", broken), ("b", broken)])

    assert result["parsed"] == 0
    assert result["discovery"] == "failed"
    assert result["discovery"] != "ok"
    assert result["discovery"] != "empty"
    assert result["ingest"]["seen"] == 0


def test_off_mode_makes_zero_source_calls(tmp_path: Path, monkeypatch):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})  # OFF default
    assert not settings.autopilot_enabled
    settings.ensure_directories()
    from app.security.crypto import CryptoBox
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, db=db,
                                                crypto=crypto,
                                                navigator=MagicMock()))
    calls: list[str] = []

    def counting(name):
        def fetch():
            calls.append(name)
            return []
        return fetch

    monkeypatch.setattr("app.scouting.trackr.scrape_folder",
                        lambda folder: calls.append("scrape") or [])
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes",
                        lambda: calls.append("live") or [])
    result = scheduler._run_sweep_locked(
        app, public_sources=[("a", counting("a")), ("b", counting("b"))])

    assert calls == []
    assert result.get("skipped") is True
    assert result.get("reason") == "automation_off"
    assert result["public_sources"] == "disabled"
    assert result["ingest"] == "disabled"


def test_lock_contention_makes_no_source_calls(tmp_path: Path):
    from app.scouting.sweep_lock import SweepLock
    settings = _review_only_settings(tmp_path)
    from app.security.crypto import CryptoBox
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, db=db,
                                                crypto=crypto,
                                                navigator=MagicMock()))
    calls: list[str] = []

    def counting():
        calls.append("public")
        return []

    holder = SweepLock(settings.data_dir / "scout-sweep.lock")
    assert holder.acquire()
    try:
        result = scheduler.run_sweep(app, public_sources=[("a", counting)])
    finally:
        holder.release()
    assert result.get("skipped") is True
    assert result.get("reason") == "sweep_locked"
    assert calls == []


def test_repeated_sweep_dedupes_without_new_applications(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.services.review_digest.send_review_digest", lambda a: True)
    injected = [("greenhouse:campaign",
                 lambda: [_row("https://boards.greenhouse.io/campaign/jobs/42")])]
    first = scheduler._run_sweep_locked(app, public_sources=injected)
    second = scheduler._run_sweep_locked(app, public_sources=injected)
    assert first["ingest"]["imported"] == 1
    assert second["ingest"]["imported"] == 0
    assert second["ingest"]["duplicates"] == 1
    with app.state.db.session_scope() as session:
        assert session.query(Opportunity).count() == 1


def test_scheduled_autopilot_never_confirms_submission(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])
    monkeypatch.setattr("app.services.review_digest.send_review_digest", lambda a: True)
    import app.automation.runner as runner_mod
    original_run = runner_mod.AutomationRunner.run
    seen_modes: list[str] = []

    def spy_run(self, application_id, mode, headed=False):
        seen_modes.append(mode.value)
        return original_run(self, application_id, mode, headed=headed)

    monkeypatch.setattr(runner_mod.AutomationRunner, "run", spy_run)
    injected = [("greenhouse:campaign",
                 lambda: [_row("https://boards.greenhouse.io/campaign/jobs/77")])]
    result = scheduler._run_sweep_locked(app, public_sources=injected)
    assert "submit" not in seen_modes
    assert result["autopilot"]["submitted"] == 0


def test_http_timeout_preserved_and_no_extra_threads():
    from app.scouting.sources import base
    import threading
    assert base.HTTP_TIMEOUT == 20
    before = threading.active_count()
    items, report = collect_all_report(sources=[("a", lambda: []), ("b", lambda: [])])
    assert items == []
    assert threading.active_count() <= before + 1
