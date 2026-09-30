"""Luna21 campaign: scheduled sweep end-to-end with cancellation, all/partial/empty
discovery, diagnostics, total-failure counters, known registrations, duplicates,
invalid budgets, malformed entries.

Injected callables only; no real network, no browser, temp DBs.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.db import Database
from app.scouting import scheduler
from app.scouting.sources.base import collect_all_report
from app.scouting.trackr import ScrapedOpportunity


def _row(
    url: str, role: str = "Summer Analyst", employer: str = "Luna21 Bank",
    source: str = "greenhouse:luna21"
) -> ScrapedOpportunity:
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


# --- cancellation semantics ----------------------------------------------------


def test_immediate_cancel_skips_pipeline_and_reports_partial(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    import app.automation.runner as runner_mod
    runner_calls: list[str] = []
    original_run = runner_mod.AutomationRunner.run

    def spy_run(self, application_id, mode, headed=False):
        runner_calls.append(mode.value)
        return original_run(self, application_id, mode, headed=headed)

    monkeypatch.setattr(runner_mod.AutomationRunner, "run", spy_run)

    result = scheduler._run_sweep_locked(
        app, public_sources=[("a", lambda: [_row("https://boards.greenhouse.io/luna21/jobs/1")])],
        is_cancelled=lambda: True)

    assert result["discovery"] == "partial"
    assert result["parsed"] == 0
    assert result["ingest"] == "cancelled"
    assert result["autopilot"]["cancelled"] == "scheduler_stopping"
    assert runner_calls == []
    with app.state.db.session_scope() as session:
        from app.models import Opportunity
        assert session.query(Opportunity).count() == 0


def test_mid_harvest_cancel_collects_first_source_then_stops(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    import app.automation.runner as runner_mod
    runner_calls: list[str] = []
    original_run = runner_mod.AutomationRunner.run

    def spy_run(self, application_id, mode, headed=False):
        runner_calls.append(mode.value)
        return original_run(self, application_id, mode, headed=headed)

    monkeypatch.setattr(runner_mod.AutomationRunner, "run", spy_run)

    state = {"fetched": 0}

    def fetch_one():
        state["fetched"] += 1
        return [_row("https://boards.greenhouse.io/luna21/jobs/1")]

    def fetch_two():
        state["fetched"] += 1
        return [_row("https://boards.greenhouse.io/luna21/jobs/2")]

    result = scheduler._run_sweep_locked(
        app, public_sources=[("one", fetch_one), ("two", fetch_two)],
        is_cancelled=lambda: state["fetched"] >= 1)

    assert result["discovery"] == "partial"
    assert result["parsed"] == 1
    assert result["ingest"] == "cancelled"
    assert runner_calls == []


# --- all/partial/empty end-to-end ---------------------------------------------


def test_all_sources_failed_is_failed_not_empty(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def broken():
        raise RuntimeError("board down")

    result = scheduler._run_sweep_locked(
        app, public_sources=[("a", broken), ("b", broken)])

    assert result["parsed"] == 0
    assert result["discovery"] == "failed"
    assert result["discovery"] != "ok"
    assert result["discovery"] != "empty"
    assert result["public_sources"]["failed"] == 2
    assert result["public_sources"]["succeeded"] == 0


def test_partial_with_rows_is_partial(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def broken():
        raise RuntimeError("board down")

    result = scheduler._run_sweep_locked(
        app, public_sources=[("ok", lambda: [_row("https://boards.greenhouse.io/luna21/jobs/1")]),
                             ("bad", broken)])

    assert result["parsed"] == 1
    assert result["discovery"] == "partial"
    assert result["public_sources"]["failed"] == 1
    assert result["public_sources"]["succeeded"] == 1


def test_multiple_empty_successes_is_empty(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    result = scheduler._run_sweep_locked(
        app, public_sources=[("ok", lambda: []),
                             ("also_empty", lambda: [])])

    assert result["parsed"] == 0
    assert result["discovery"] == "empty"
    assert result["public_sources"]["attempted"] == 2
    assert result["public_sources"]["succeeded"] == 2
    assert result["public_sources"]["failed"] == 0


def test_all_empty_sources_is_empty(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    result = scheduler._run_sweep_locked(app, public_sources=[("a", lambda: [])])
    assert result["parsed"] == 0
    assert result["discovery"] == "empty"


def test_healthy_sources_is_ok(tmp_path: Path, monkeypatch):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    result = scheduler._run_sweep_locked(
        app, public_sources=[("greenhouse:luna21", lambda: [
            _row("https://boards.greenhouse.io/luna21/jobs/8")])])
    assert result["discovery"] == "ok"
    assert result["ingest"]["imported"] == 1


# --- diagnostics: error sanitization ------------------------------------------


def test_aggregator_total_failure_counters_and_sanitization(tmp_path: Path, monkeypatch):
    """Aggregator source whose every page raises: registered=attempted=failed=1,
    succeeded=0, discovery=failed, list-compatible public result remains [],
    no raw URL/canary in logs/errors."""
    from app.scouting.sources.base import PartialFetchResult

    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def aggregator_all_fail():
        return PartialFetchResult(
            [],
            failed_pages=3,
            total_pages=3,
            failed_urls=("https://example.com/page1", "https://example.com/page2", "https://example.com/page3"),
            collector_error="RuntimeError",
        )

    result = scheduler._run_sweep_locked(app, public_sources=[("aggregator", aggregator_all_fail)])

    report = result["public_sources"]
    assert report["registered"] == 1
    assert report["attempted"] == 1
    assert report["succeeded"] == 0
    assert report["failed"] == 1
    assert report["per_source"] == {"aggregator": 0}
    assert report["errors"]["aggregator"] == "RuntimeError"
    # No raw URL in error
    assert "example.com" not in str(report["errors"])
    assert result["discovery"] == "failed"
    assert result["parsed"] == 0


def test_aggregator_partial_failure_reported(tmp_path: Path, monkeypatch):
    """Aggregator with some failed pages: partial_sources reported, error sanitized."""
    from app.scouting.sources.base import PartialFetchResult

    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def aggregator_partial():
        return PartialFetchResult(
            [_row("https://boards.greenhouse.io/luna21/jobs/1")],
            failed_pages=1,
            total_pages=3,
            failed_urls=("https://example.com/failed",),
        )

    result = scheduler._run_sweep_locked(app, public_sources=[("aggregator", aggregator_partial)])

    report = result["public_sources"]
    assert report["registered"] == 1
    assert report["attempted"] == 1
    assert report["succeeded"] == 1
    assert report["failed"] == 0
    assert "aggregator" in report["partial_sources"]
    assert "partial:" in report["errors"]["aggregator"]
    assert "example.com" not in str(report["errors"])
    assert result["discovery"] == "partial"


def test_throwing_collector_error_fallback_does_not_mask_healthy(tmp_path: Path, monkeypatch):
    """Separate throwing-collector test so error fallback cannot make healthy-route proof pass."""
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def boom(settings=None, sources=None, **kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("app.scouting.sources.base.collect_all_report", boom)

    result = scheduler._run_sweep_locked(app)

    assert result["discovery"] == "failed"
    assert result["parsed"] == 0
    assert result["ingest"]["seen"] == 0
    assert "collector_error" in result["public_sources"]
    assert "RuntimeError" in result["public_sources"]["collector_error"]


# --- known registrations: invalid/negative budgets retain known source count ---


def test_invalid_budget_retains_known_source_count():
    def counting():
        return []

    items, report = collect_all_report(sources=[("a", counting), ("b", counting)], deadline_seconds="invalid")
    assert items == []
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert report["failed"] == 0
    assert report["skipped"] == ["a", "b"]
    assert report["truncated"] == "time_budget"
    assert "collector_error" in report


def test_negative_budget_retains_known_source_count():
    def counting():
        return []

    items, report = collect_all_report(sources=[("a", counting), ("b", counting)], deadline_seconds=-1)
    assert items == []
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert report["failed"] == 0
    assert report["skipped"] == ["a", "b"]
    assert report["truncated"] == "time_budget"


def test_nan_budget_retains_known_source_count():
    def counting():
        return []

    items, report = collect_all_report(sources=[("a", counting), ("b", counting)], deadline_seconds=float("nan"))
    assert items == []
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert report["truncated"] == "time_budget"


def test_inf_budget_retains_known_source_count():
    def counting():
        return []

    items, report = collect_all_report(sources=[("a", counting), ("b", counting)], deadline_seconds=float("inf"))
    assert items == []
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert report["truncated"] == "time_budget"


# --- duplicates reported even if skipped by zero budget/cancellation ------------


def test_zero_budget_duplicates_reported():
    def counting():
        return []

    items, report = collect_all_report(sources=[("dup", counting), ("dup", counting)], deadline_seconds=0)
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert "dup" in report["duplicate_sources"]
    assert report["skipped"] == ["dup", "dup"]


def test_cancelled_duplicates_reported():
    def counting():
        return []

    state = {"count": 0}
    def cancel_after_0():
        state["count"] += 1
        return state["count"] > 0

    items, report = collect_all_report(sources=[("dup", counting), ("dup", counting)], is_cancelled=cancel_after_0)
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert "dup" in report["duplicate_sources"]
    assert report["skipped"] == ["dup", "dup"]


# --- malformed specs rejected without counting as attempted --------------------


def test_malformed_spec_rejected_not_attempted():
    def good():
        return []

    items, report = collect_all_report(sources=[("good", good), ("bad", "not callable")])
    assert report["registered"] == 2
    assert report["attempted"] == 1
    assert report["succeeded"] == 1
    assert report["failed"] == 0
    assert "<malformed:1>" in report["errors"]
    assert "malformed" in report["errors"]["<malformed:1>"]


def test_malformed_spec_not_a_tuple():
    def good():
        return []

    items, report = collect_all_report(sources=[("good", good), "not a tuple"])
    assert report["registered"] == 2
    assert report["attempted"] == 1
    assert report["failed"] == 0
    assert "<malformed:1>" in report["errors"]


def test_malformed_spec_wrong_arity():
    def good():
        return []

    items, report = collect_all_report(sources=[("good", good), ("bad", lambda: [], "extra")])
    assert report["registered"] == 2
    assert report["attempted"] == 1
    assert report["failed"] == 0
    assert "<malformed:1>" in report["errors"]


def test_malformed_spec_log_sanitized(caplog):
    """Malformed registration log should not leak callable repr or synthetic canary."""
    import logging
    from app.scouting.sources.base import collect_all_report

    def good():
        return []

    with caplog.at_level(logging.WARNING):
        collect_all_report(sources=[("good", good), ("bad", "not callable")])

    # Check that the log doesn't contain the callable repr or full spec
    warning_logs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    malformed_logs = [msg for msg in warning_logs if "malformed" in msg.lower()]
    assert len(malformed_logs) >= 1
    # Should not contain the string representation of the callable
    assert "not callable" not in str(malformed_logs)
    # Should only mention type and index
    assert "str" in str(malformed_logs) or "tuple" in str(malformed_logs)


# --- positive controls with real collector invocation --------------------------


def test_digest_positive_with_collector_invocation(tmp_path: Path, monkeypatch):
    """Digest positive test with actual collector invocation, discovery/report
    and autopilot/digest assertions."""
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)

    # Use real collect_all_report with injected sources
    injected = [("greenhouse:campaign",
                  lambda: [_row("https://boards.greenhouse.io/campaign/jobs/9")])]

    digest_calls = []
    def fake_send(a):
        digest_calls.append(a)
        return True
    monkeypatch.setattr("app.services.review_digest.send_review_digest", fake_send)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])

    result = scheduler._run_sweep_locked(app, public_sources=injected)

    # Discovery and report assertions
    assert result["public_sources"]["succeeded"] == 1
    assert result["public_sources"]["failed"] == 0
    assert result["parsed"] == 1
    assert result["discovery"] == "ok"
    assert result["ingest"]["imported"] == 1

    # REVIEW_ONLY result uses service counters, not collector counters.
    assert result["autopilot"]["processed"] == 0
    assert result["autopilot"]["submitted"] == 0
    assert result["autopilot"]["mode"] == "REVIEW_ONLY"
    assert result["autopilot"]["submission_armed"] is False
    assert len(digest_calls) == 1
    assert digest_calls[0] is app


def test_digest_separate_throwing_collector_error_fallback(tmp_path: Path, monkeypatch):
    """Separate throwing-collector test so error fallback cannot make healthy-route proof pass."""
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", lambda: [])
    monkeypatch.setattr("app.scouting.trackr.scrape_folder", lambda folder: [])

    def boom(settings=None, sources=None, **kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("app.scouting.sources.base.collect_all_report", boom)

    digest_calls = []
    def fake_send(a):
        digest_calls.append(a)
        return True
    monkeypatch.setattr("app.services.review_digest.send_review_digest", fake_send)

    result = scheduler._run_sweep_locked(app)

    # Collector error should be handled, digest still called
    assert result["discovery"] == "failed"
    assert result["public_sources"]["collector_error"] is not None
    assert "RuntimeError" in result["public_sources"]["collector_error"]
    assert len(digest_calls) == 1