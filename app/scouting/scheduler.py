"""Background daily sweep for ARGUS.

Runs inside the app process: collects the existing public HTTP discovery
sources (same ``collect_all`` feed as manual full-sweep, truthful per-source
reporting), then scans the watched Trackr saves folder and optional live
Trackr scrape as supplements, ingests new opportunities and drives the
autopilot on an explicitly enabled interval, plus once shortly after boot.
A zero/non-positive interval or OFF mode disables the scheduler entirely.
Failures are logged, never fatal - a broken sweep must not take the
dashboard down. HTTP harvest is discovery-only; target/CV/eligibility/
consent gates still apply downstream.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import MIN_SWEEP_INTERVAL_SECONDS
from app.scouting.sweep_lock import SweepLock

logger = logging.getLogger(__name__)

_STOP = threading.Event()
_THREAD: threading.Thread | None = None

# Bounded public harvest: a cooperative between-source budget, not a strict
# whole-sweep guarantee. One active source may itself issue several HTTP
# calls, so a source already running can overrun this bound; each individual
# call still honors the shared per-request HTTP_TIMEOUT. There is
# deliberately no threadpool, so the bound can never preempt inflight work —
# it only stops further sources from starting.
_PUBLIC_HARVEST_BUDGET_SECONDS = 300.0


def _stop_requested(is_cancelled) -> bool:  # noqa: ANN001 - optional callback
    """True when an explicit shutdown was requested; fail closed on misuse."""

    if is_cancelled is None:
        return False
    try:
        return bool(is_cancelled())
    except Exception:
        return True


def trackr_folder(data_dir: Path) -> Path:
    folder = data_dir / "trackr_saves"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def run_sweep(app, *, public_sources=None, is_cancelled=None) -> dict[str, object]:  # noqa: ANN001 - FastAPI app instance
    """One sweep pass: public sources + live Trackr scrape -> ingest -> autopilot."""
    with _SWEEP_LOCK:
        settings = app.state.settings
        lock = SweepLock(settings.data_dir / "scout-sweep.lock")
        if not lock.acquire():
            return {
                "at": datetime.now(timezone.utc).isoformat(),
                "skipped": True,
                "reason": "sweep_locked",
            }
        try:
            return _run_sweep_locked(
                app, public_sources=public_sources, is_cancelled=is_cancelled
            )
        finally:
            lock.release()


_SWEEP_LOCK = threading.Lock()

def _run_sweep_locked(app, *, public_sources=None, is_cancelled=None) -> dict[str, object]:  # noqa: ANN001 - FastAPI app instance
    """One sweep pass: public HTTP discovery + Trackr supplement -> ingest -> autopilot."""
    settings = app.state.settings
    results: dict[str, object] = {"at": datetime.now(timezone.utc).isoformat()}
    if not settings.autopilot_enabled:
        # OFF is a hard scheduler stop: do not fetch public sources, live
        # Trackr data, saved pages, or invoke the review/automation pipeline.
        results.update(
            {
                "skipped": True,
                "reason": "automation_off",
                "live_scrape": "disabled",
                "parsed": 0,
                "public_sources": "disabled",
                "per_source": {},
                "discovery": "disabled",
                "ingest": "disabled",
                "autopilot": {
                    "mode": settings.automation_mode.value,
                    "disabled_reason": "automation_off",
                },
            }
        )
        return results

    from app.scouting.service import ScoutService
    from app.scouting.sources.base import collect_all_report
    from app.scouting.trackr import scrape_folder
    from app.scouting.trackr_live import fetch_programmes
    from app.services.review_digest import send_review_digest

    scraped = []
    # Discovery-only public HTTP harvest (no browser, no login). Trackr live
    # and saved HTML remain optional supplements below. Existing target/CV/
    # eligibility/consent gates in ScoutService/autopilot still apply.
    # The harvest is time-bounded and cancellable; truncation is reported,
    # never mistaken for empty-success.
    try:
        if public_sources is None:
            public_items, public_report = collect_all_report(
                settings,
                deadline_seconds=_PUBLIC_HARVEST_BUDGET_SECONDS,
                is_cancelled=is_cancelled,
            )
        else:
            public_items, public_report = collect_all_report(
                settings,
                sources=public_sources,
                deadline_seconds=_PUBLIC_HARVEST_BUDGET_SECONDS,
                is_cancelled=is_cancelled,
            )
        scraped.extend(public_items)
        results["public_sources"] = public_report
    except Exception as exc:  # noqa: BLE001 - public harvest failing must not kill the sweep
        # Log the exception type only: the raw message/traceback can embed
        # request URLs or credentials. The failure itself stays visible via
        # collector_error below and the failed discovery status.
        logger.warning(
            "public source harvest failed (%s); continuing with Trackr supplement",
            type(exc).__name__,
        )
        public_report = {
            "registered": 0,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "per_source": {},
            "errors": {"collector": "public harvest failed - see server log"},
            "total_raw": 0,
            "total": 0,
            "skipped": [],
            "truncated": None,
            "duplicate_sources": [],
            "collector_error": f"{type(exc).__name__}: public harvest failed",
        }
        results["public_sources"] = public_report
    if _stop_requested(is_cancelled):
        results["parsed"] = len(scraped)
        per_source: dict[str, int] = {}
        for item in scraped:
            key = str(getattr(item, "source", "unknown")).split(":", 1)[0]
            per_source[key] = per_source.get(key, 0) + 1
        results["per_source"] = per_source
        results["discovery"] = "partial"
        results["ingest"] = "cancelled"
        results["autopilot"] = {
            "mode": settings.automation_mode.value,
            "cancelled": "scheduler_stopping",
        }
        return results
    if settings.trackr_live_enabled:
        try:
            scraped.extend(fetch_programmes())
            results["live_scrape"] = len(scraped) - int(public_report.get("total", 0))
        except Exception:  # noqa: BLE001 - live scrape failing must not kill the sweep
            logger.exception("trackr live scrape failed; falling back to saved HTML")
            results["live_scrape"] = "failed"
    else:
        results["live_scrape"] = "disabled"
    if _stop_requested(is_cancelled):
        results["parsed"] = len(scraped)
        per_source: dict[str, int] = {}
        for item in scraped:
            key = str(getattr(item, "source", "unknown")).split(":", 1)[0]
            per_source[key] = per_source.get(key, 0) + 1
        results["per_source"] = per_source
        results["discovery"] = "partial"
        results["ingest"] = "cancelled"
        results["autopilot"] = {
            "mode": settings.automation_mode.value,
            "cancelled": "scheduler_stopping",
        }
        return results

    # saved-HTML drops remain supported as an override/supplement
    scraped.extend(scrape_folder(trackr_folder(settings.data_dir)))
    results["parsed"] = len(scraped)
    per_source: dict[str, int] = {}
    for item in scraped:
        key = str(getattr(item, "source", "unknown")).split(":", 1)[0]
        per_source[key] = per_source.get(key, 0) + 1
    results["per_source"] = per_source
    public_failed = int(public_report.get("failed", 0))
    public_succeeded = int(public_report.get("succeeded", 0))
    public_attempted = int(public_report.get("attempted", 0))
    collector_error = public_report.get("collector_error")
    truncated = public_report.get("truncated")
    partial_sources = public_report.get("partial_sources", [])
    if truncated in ("time_budget", "cancelled"):
        # Bounded-harvest stop or operator cancellation with rows unattempted:
        # explicitly partial whatever was collected, never empty-success.
        results["discovery"] = "partial"
    elif results["parsed"] == 0 and (
        collector_error or (public_attempted > 0 and public_succeeded == 0)
    ):
        # Collector-level failure, or every source failed, and no Trackr rows:
        # a failure, never an empty-success.
        results["discovery"] = "failed"
    elif collector_error or public_failed > 0 or partial_sources or results["live_scrape"] == "failed":
        results["discovery"] = "partial"
    elif results["parsed"] == 0:
        results["discovery"] = "empty"
    else:
        results["discovery"] = "ok"
    if _stop_requested(is_cancelled):
        # Explicit shutdown during or after harvest: honor stop intent and do
        # not touch the database, navigator, autopilot, or digest. Already
        # collected rows are deliberately left uningested for the next pass.
        results["ingest"] = "cancelled"
        results["autopilot"] = {
            "mode": settings.automation_mode.value,
            "cancelled": "scheduler_stopping",
        }
        return results
    try:
        with app.state.db.session_scope() as session:
            scout = ScoutService(session, settings, app.state.crypto)
            navigator = getattr(app.state, "navigator", None)
            if navigator is None:
                raise RuntimeError(
                    "ARGUS application navigator is unavailable for scheduled automation"
                )

            def runner_factory(application_id: str, mode, headed: bool):  # noqa: ANN001
                from dataclasses import asdict

                from app.automation.runner import AutomationRunner
                from app.automation.types import RunMode

                outcome = AutomationRunner(
                    app.state.db,
                    settings,
                    app.state.crypto,
                    handoff_manager=navigator,
                ).run(application_id, RunMode(mode), headed=headed)
                payload = asdict(outcome)
                receipt = payload.get("receipt")
                payload["receipt"] = (
                    {k: v for k, v in receipt.items() if v} if receipt else None
                )
                return payload

            results["ingest"] = scout.ingest(scraped)
            results["autopilot"] = scout.run_autopilot(runner_factory, max_runs=10)
            try:
                send_review_digest(app)
            except Exception:  # noqa: BLE001 - digest failure must never break or mask the sweep's own result
                logger.exception("review digest failed")
    except Exception:  # noqa: BLE001
        logger.exception("scout sweep failed")
        results["error"] = "sweep failed - see server log"
    return results


def _current_season() -> str:
    """Trackr season label: applications for next academic cycle."""
    now = datetime.now(timezone.utc)
    # UK early-careers season runs Aug-Jul; from August we target next year.
    season_year = now.year + 1 if now.month >= 8 else now.year + 1
    return str(min(season_year, 2028))


def _positive_interval_seconds(interval_hours: float) -> float | None:
    """Return a safe one-second-minimum wait, or ``None`` when disabled."""

    try:
        interval = float(interval_hours)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(interval) or interval <= 0:
        return None
    interval_seconds = interval * 3600
    if not math.isfinite(interval_seconds):
        return None
    if interval_seconds < MIN_SWEEP_INTERVAL_SECONDS:
        return None
    return interval_seconds


def _scheduler_enabled(app) -> bool:  # noqa: ANN001 - FastAPI app instance
    """Require an explicit non-OFF automation state before scheduling."""

    state = getattr(app, "state", None)
    settings = getattr(state, "settings", None)
    return bool(getattr(settings, "autopilot_enabled", False))


def _loop(app, interval_hours: float) -> None:  # noqa: ANN001
    interval_seconds = _positive_interval_seconds(interval_hours)
    if interval_seconds is None or not _scheduler_enabled(app):
        return

    # First pass shortly after boot so an explicitly enabled dashboard has
    # fresh data.  OFF/disabled configurations never create this thread, and
    # the guard below also protects direct callers of this private function.
    _STOP.wait(20)
    while not _STOP.is_set() and _scheduler_enabled(app):
        try:
            # Cancellation comes only from this owned loop: the global _STOP
            # may be set during an ordinary manual run, so run_sweep callers
            # outside the loop get no callback by default.
            outcome = run_sweep(app, is_cancelled=_STOP.is_set)
            logger.info("scout sweep: %s", outcome)
        except Exception:  # noqa: BLE001
            logger.exception("unexpected sweep failure")
        if _STOP.wait(interval_seconds):
            break


def start_scheduler(app, interval_hours: float = 0.0) -> None:  # noqa: ANN001
    global _THREAD
    if _positive_interval_seconds(interval_hours) is None or not _scheduler_enabled(app):
        # Setting the event also asks an already-running scheduler to stop if
        # a caller disables it at runtime.  No thread is created for this
        # invocation, and no boot sweep can occur.
        _STOP.set()
        return
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop,
        args=(app, interval_hours),
        name="argus-scout-sweep",
        daemon=True,
    )
    _THREAD.start()
    logger.info("scout scheduler started (interval %.1fh)", interval_hours)


def stop_scheduler() -> None:
    _STOP.set()
