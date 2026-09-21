"""Follow-up round 2: registered/attempted honesty, invalid-budget refusal,
fail-closed cancellation, whitelist error redaction, full-attribute row
validation, shutdown-cancel skips pipeline, collector log redaction.

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


def _row(url: str, role: str = "Summer Analyst", employer: str = "Round2 Bank",
         source: str = "greenhouse:round2") -> ScrapedOpportunity:
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


def _two_valid_sources():
    return [("one", lambda: [_row("https://boards.greenhouse.io/round2/jobs/1")]),
            ("two", lambda: [_row("https://boards.greenhouse.io/round2/jobs/2")])]


# --- registered vs genuinely attempted ---------------------------------------


def test_zero_budget_reports_registered_not_attempted():
    items, report = collect_all_report(sources=_two_valid_sources(), deadline_seconds=0)
    assert items == []
    assert report["registered"] == 2
    assert report["attempted"] == 0
    assert report["succeeded"] == 0
    assert report["failed"] == 0
    assert report["skipped"] == ["one", "two"]
    assert report["truncated"] == "time_budget"


def test_attempted_equals_succeeded_plus_failed():
    def broken():
        raise RuntimeError("down")

    items, report = collect_all_report(
        sources=[("ok", lambda: [_row("https://boards.greenhouse.io/round2/jobs/1")]),
                 ("bad", broken)])
    assert report["registered"] == 2
    assert report["attempted"] == report["succeeded"] + report["failed"] == 2


# --- invalid budget refuses without fetching ----------------------------------


def test_invalid_budget_never_fetches_and_reports_error():
    calls: list[str] = []

    def counting():
        calls.append("fetch")
        return []

    for bad in ("invalid", -1, float("nan"), float("inf")):
        items, report = collect_all_report(sources=[("a", counting)],
                                           deadline_seconds=bad)
        assert items == []
        assert calls == []
        assert report["attempted"] == 0
        assert report["collector_error"] is not None


# --- fail-closed cancellation --------------------------------------------------


def test_raising_cancel_callback_fetches_nothing_and_reports():
    calls: list[str] = []

    def counting():
        calls.append("fetch")
        return []

    def broken_cancel():
        raise RuntimeError("stop guard exploded")

    items, report = collect_all_report(sources=[("a", counting), ("b", counting)],
                                       is_cancelled=broken_cancel)
    assert items == []
    assert calls == []
    assert report["truncated"] == "cancelled"
    assert report["skipped"] == ["a", "b"]
    assert "cancellation_callback" in report["errors"]


# --- whitelist redaction: no arbitrary message ---------------------------------


def test_non_url_secret_message_reduced_to_type_only():
    def leaky():
        raise RuntimeError("Authorization Bearer SYNTHETIC_CANARY_NOT_A_CREDENTIAL")

    items, report = collect_all_report(sources=[("leaky", leaky)])
    assert items == []
    assert report["errors"]["leaky"] == "RuntimeError"
    assert "SYNTHETIC_CANARY_NOT_A_CREDENTIAL" not in report["errors"]["leaky"]


def test_http_status_diagnostics_preserved():
    class FakeResponse:
        status_code = 401

    class FakeHTTPError(RuntimeError):
        def __init__(self):
            super().__init__("401 from https://provider.example/secret?token=abc")
            self.response = FakeResponse()

    _, report = collect_all_report(sources=[("auth", lambda: (_ for _ in ()).throw(FakeHTTPError()))])
    assert report["errors"]["auth"] == "FakeHTTPError: HTTP 401"


# --- full-attribute row validation ----------------------------------------------


def test_non_string_identity_attribute_rejected_in_isolation():
    good = _row("https://boards.greenhouse.io/round2/jobs/1")
    bad = _row("https://boards.greenhouse.io/round2/jobs/2")
    bad.employer = 123  # type: ignore[assignment]

    items, report = collect_all_report(
        sources=[("good", lambda: [good]), ("bad", lambda: [bad])])
    assert [m.url for m in items] == ["https://boards.greenhouse.io/round2/jobs/1"]
    assert report["succeeded"] == 1
    assert report["failed"] == 1
    assert "bad" in report["errors"]


# --- shutdown cancel skips later pipeline ---------------------------------------


def test_immediate_cancel_skips_ingest_and_autopilot(tmp_path: Path, monkeypatch, caplog):
    import app.automation.runner as runner_mod
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)
    runner_calls: list[str] = []
    original_run = runner_mod.AutomationRunner.run

    def spy_run(self, application_id, mode, headed=False):
        runner_calls.append(mode.value)
        return original_run(self, application_id, mode, headed=headed)

    monkeypatch.setattr(runner_mod.AutomationRunner, "run", spy_run)
    with caplog.at_level("WARNING"):
        result = scheduler._run_sweep_locked(
            app, public_sources=_two_valid_sources(), is_cancelled=lambda: True)
    assert result["discovery"] == "partial"
    assert result["parsed"] == 0
    assert result["ingest"] == "cancelled"
    assert result["autopilot"]["cancelled"] == "scheduler_stopping"
    assert runner_calls == []
    with app.state.db.session_scope() as session:
        from app.models import Opportunity
        assert session.query(Opportunity).count() == 0


def test_mid_harvest_cancel_does_not_ingest_collected_rows(tmp_path: Path, monkeypatch):
    import app.automation.runner as runner_mod
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)
    runner_calls: list[str] = []
    original_run = runner_mod.AutomationRunner.run

    def spy_run(self, application_id, mode, headed=False):
        runner_calls.append(mode.value)
        return original_run(self, application_id, mode, headed=headed)

    monkeypatch.setattr(runner_mod.AutomationRunner, "run", spy_run)
    state = {"fetched": 0}

    def fetch_one():
        state["fetched"] += 1
        return [_row("https://boards.greenhouse.io/round2/jobs/1")]

    def fetch_two():
        state["fetched"] += 1
        return [_row("https://boards.greenhouse.io/round2/jobs/2")]

    result = scheduler._run_sweep_locked(
        app, public_sources=[("one", fetch_one), ("two", fetch_two)],
        is_cancelled=lambda: state["fetched"] >= 1)
    assert result["discovery"] == "partial"
    assert result["ingest"] == "cancelled"
    assert runner_calls == []
    with app.state.db.session_scope() as session:
        from app.models import Opportunity
        assert session.query(Opportunity).count() == 0


# --- collector log redaction -----------------------------------------------------


def test_scheduler_collector_failure_hides_raw_url_but_reports_failure(
        tmp_path: Path, monkeypatch, caplog):
    settings = _review_only_settings(tmp_path)
    app = _mock_app(tmp_path, settings)
    _stub_trackr_empty(monkeypatch)

    def boom(settings=None, sources=None, **kwargs):
        raise RuntimeError("401 from https://provider.example/secret?token=abc")

    monkeypatch.setattr("app.scouting.sources.base.collect_all_report", boom)
    with caplog.at_level("WARNING"):
        result = scheduler._run_sweep_locked(app)
    assert result["discovery"] == "failed"
    assert "https://provider.example" not in caplog.text
    assert "token=abc" not in caplog.text
