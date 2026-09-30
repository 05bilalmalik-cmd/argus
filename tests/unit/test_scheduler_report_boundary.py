"""Report-boundary follow-up: collector-level failure, row isolation, error
redaction, duplicate names, bounded harvest/cancellation.

All sources are injected callables; no real network, no browser, temp DBs.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.db import Database
from app.scouting import scheduler
from app.scouting.sources.base import collect_all, collect_all_report
from app.scouting.trackr import ScrapedOpportunity


def _row(url: str, role: str = "Summer Analyst", employer: str = "Boundary Bank",
         source: str = "greenhouse:boundary") -> ScrapedOpportunity:
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
    return SimpleNamespace(state=SimpleNamespace(settings=settings, db=db,
                                                 crypto=crypto,
                                                 navigator=MagicMock()))


def _stub_trackr_empty(monkeypatch, saved=()):
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: list(saved))
    monkeypatch.setattr("app.services.review_digest.send_review_digest", lambda a: True)


# --- collector-level throw must not become empty/ok --------------------------


def test_collector_throw_with_no_trackr_rows_is_failed(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def boom(settings=None, sources=None, **kwargs):
        raise RuntimeError("source construction exploded")

    monkeypatch.setattr("app.scouting.sources.base.collect_all_report", boom)
    result = scheduler._run_sweep_locked(app)

    assert result["parsed"] == 0
    assert result["discovery"] == "failed"
    assert result["ingest"]["seen"] == 0


def test_collector_throw_with_saved_rows_is_partial_and_ingests(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    saved = [_row("https://boards.greenhouse.io/boundary/jobs/5")]
    _stub_trackr_empty(monkeypatch, saved=saved)

    def boom(settings=None, sources=None, **kwargs):
        raise RuntimeError("source construction exploded")

    monkeypatch.setattr("app.scouting.sources.base.collect_all_report", boom)
    result = scheduler._run_sweep_locked(app)

    assert result["parsed"] == 1
    assert result["discovery"] == "partial"
    assert result["ingest"]["imported"] == 1


# --- malformed rows isolated per source --------------------------------------


def test_generator_row_failure_does_not_discard_earlier_good_source():
    good = _row("https://boards.greenhouse.io/boundary/jobs/1")

    def flaky():
        yield _row("https://jobs.lever.co/boundary/2", role="Spring Week",
                   source="lever:boundary")
        raise RuntimeError("mid-iteration transport failure")

    items, report = collect_all_report(
        sources=[("good", lambda: [good]), ("flaky", flaky)])
    assert [m.url for m in items] == ["https://boards.greenhouse.io/boundary/jobs/1"]
    assert report["succeeded"] == 1
    assert report["failed"] == 1
    assert set(report["errors"]) == {"flaky"}


def test_malformed_row_source_rejected_without_losing_good_source():
    good = _row("https://boards.greenhouse.io/boundary/jobs/1")

    class NotARow:
        pass

    items, report = collect_all_report(
        sources=[("good", lambda: [good]), ("bad", lambda: [NotARow()])])
    assert [m.url for m in items] == ["https://boards.greenhouse.io/boundary/jobs/1"]
    assert report["failed"] == 1
    assert "bad" in report["errors"]


def test_collect_all_rejects_source_whose_iteration_fails():
    """A source that fails mid-iteration is rejected whole: partial rows from
    a failed fetch are suspect, so the source contributes nothing rather than
    permissive partial defaults. Earlier good sources are unaffected (above)."""
    good = _row("https://boards.greenhouse.io/boundary/jobs/1")

    def flaky():
        yield good
        raise RuntimeError("late failure")

    merged = collect_all(sources=[("flaky", flaky)])
    assert merged == []
    _, report = collect_all_report(sources=[("flaky", flaky)])
    assert report["failed"] == 1
    assert "flaky" in report["errors"]


# --- error redaction ----------------------------------------------------------


def test_source_error_redacts_urls_and_keeps_type():
    def leaky():
        raise RuntimeError("HTTP 401 from https://api.example.com/jobs?token=secret-abc")

    items, report = collect_all_report(sources=[("leaky", leaky)])
    assert items == []
    message = report["errors"]["leaky"]
    assert "RuntimeError" in message
    assert "secret-abc" not in message
    assert "https://api.example.com" not in message


# --- duplicate source names ----------------------------------------------------


def test_duplicate_source_names_are_explicitly_reported():
    first = _row("https://boards.greenhouse.io/boundary/jobs/1")
    second = _row("https://boards.greenhouse.io/boundary/jobs/2")
    items, report = collect_all_report(
        sources=[("dup", lambda: [first]), ("dup", lambda: [second])])
    assert report["attempted"] == 2
    assert report["succeeded"] == 2
    assert "dup" in report["duplicate_sources"]
    assert "duplicate_sources" in report["errors"]
    assert report["per_source"]["dup"] == 2
    assert len(items) == 2


# --- bounded harvest / cancellation --------------------------------------------


def test_zero_budget_skips_all_sources_as_truncated():
    items, report = collect_all_report(
        sources=[("a", lambda: [_row("https://boards.greenhouse.io/boundary/jobs/1")]),
                 ("b", lambda: [_row("https://boards.greenhouse.io/boundary/jobs/2")])],
        deadline_seconds=0)
    assert items == []
    assert report["truncated"] == "time_budget"
    assert report["skipped"] == ["a", "b"]
    assert report["succeeded"] == 0


def test_cancellation_after_first_source_keeps_its_rows_and_skips_rest():
    state = {"fetched": 0}

    def fetch_a():
        state["fetched"] += 1
        return [_row("https://boards.greenhouse.io/boundary/jobs/1")]

    def fetch_b():
        state["fetched"] += 1
        return [_row("https://boards.greenhouse.io/boundary/jobs/2")]

    items, report = collect_all_report(
        sources=[("a", fetch_a), ("b", fetch_b)],
        is_cancelled=lambda: state["fetched"] >= 1)
    assert [m.url for m in items] == ["https://boards.greenhouse.io/boundary/jobs/1"]
    assert report["truncated"] == "cancelled"
    assert report["skipped"] == ["b"]
    assert state["fetched"] == 1


def test_scheduler_cancellation_is_partial_never_empty_success(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)
    calls: list[str] = []

    def counting():
        calls.append("public")
        return []

    result = scheduler._run_sweep_locked(
        app, public_sources=[("a", counting)], is_cancelled=lambda: True)
    assert calls == []
    assert result["parsed"] == 0
    assert result["discovery"] == "partial"


# --- positive controls ----------------------------------------------------------


def test_scheduler_all_empty_sources_is_empty(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)
    result = scheduler._run_sweep_locked(app, public_sources=[("a", lambda: [])])
    assert result["parsed"] == 0
    assert result["discovery"] == "empty"


def test_scheduler_unchanged_success_is_ok(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)
    result = scheduler._run_sweep_locked(
        app, public_sources=[("greenhouse:boundary", lambda: [
            _row("https://boards.greenhouse.io/boundary/jobs/8")])])
    assert result["discovery"] == "ok"
    assert result["ingest"]["imported"] == 1
