"""Background daily sweep for ARGUS.

Runs inside the app process: scans the watched Trackr saves folder, ingests
new opportunities and drives the autopilot on an explicitly enabled interval,
plus once shortly after boot. A zero/non-positive interval or OFF mode disables
the scheduler entirely. Failures are logged, never fatal - a broken sweep must
not take the dashboard down.
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


def trackr_folder(data_dir: Path) -> Path:
    folder = data_dir / "trackr_saves"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


SWEEP_HEALTH_FILENAME = "sweep_health.json"
_SWEEP_HEALTH_LIMIT_BYTES = 4096


def sweep_health_path(data_dir: Path) -> Path:
    return Path(data_dir) / SWEEP_HEALTH_FILENAME


def record_sweep_health(data_dir: Path, results: dict[str, object]) -> None:
    """Persist a bounded sweep-health snapshot (best-effort, never raises).

    Turns silent in-memory sweep degradation into visible, queryable state
    surfaced via /healthz. Only scalar summaries are kept; per-opportunity
    detail stays in the audit log, never here.
    """

    try:
        import json
        import os

        payload: dict[str, object] = {
            "at": str(results.get("at", ""))[:64],
            "skipped": bool(results.get("skipped", False)),
            "reason": str(results.get("reason", "") or "")[:120],
            "live_scrape": str(results.get("live_scrape", ""))[:64],
            "parsed": results.get("parsed", 0)
            if isinstance(results.get("parsed"), int)
            else 0,
            "error": str(results.get("error", "") or "")[:240],
        }
        ingest = results.get("ingest")
        if isinstance(ingest, dict):
            try:
                payload["ingested"] = int(ingest.get("imported", 0) or 0)
            except (TypeError, ValueError):
                payload["ingested"] = 0
            try:
                payload["ingest_duplicates"] = int(ingest.get("duplicates", 0) or 0)
            except (TypeError, ValueError):
                payload["ingest_duplicates"] = 0
        raw = json.dumps(payload, sort_keys=True)[:_SWEEP_HEALTH_LIMIT_BYTES]
        target = sweep_health_path(data_dir)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001 - health persistence must never break a sweep
        logger.warning("sweep health persist failed", exc_info=True)


def read_sweep_health(data_dir: Path) -> dict[str, object]:
    """Return the last persisted sweep-health snapshot, or {} when unknown."""

    try:
        import json

        raw = sweep_health_path(data_dir).read_text(encoding="utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 - missing/corrupt health reads as unknown
        return {}


def run_sweep(app) -> dict[str, object]:  # noqa: ANN001 - FastAPI app instance
    """One sweep pass: live Trackr scrape -> ingest -> autopilot."""
    with _SWEEP_LOCK:
        settings = app.state.settings
        lock = SweepLock(settings.data_dir / "scout-sweep.lock")
        if not lock.acquire():
            results: dict[str, object] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "skipped": True,
                "reason": "sweep_locked",
            }
            record_sweep_health(settings.data_dir, results)
            return results
        try:
            results = _run_sweep_locked(app)
            record_sweep_health(settings.data_dir, results)
            return results
        finally:
            lock.release()


_SWEEP_LOCK = threading.Lock()

def _run_sweep_locked(app) -> dict[str, object]:  # noqa: ANN001 - FastAPI app instance
    """One sweep pass: live Trackr scrape -> ingest -> autopilot."""
    settings = app.state.settings
    results: dict[str, object] = {"at": datetime.now(timezone.utc).isoformat()}
    if not settings.autopilot_enabled:
        # OFF is a hard scheduler stop: do not fetch live Trackr data, parse
        # saved pages, or invoke the review/automation pipeline.
        results.update(
            {
                "skipped": True,
                "reason": "automation_off",
                "live_scrape": "disabled",
                "parsed": 0,
                "ingest": "disabled",
                "autopilot": {
                    "mode": settings.automation_mode.value,
                    "disabled_reason": "automation_off",
                },
            }
        )
        return results

    from app.scouting.service import ScoutService
    from app.scouting.trackr import scrape_folder
    from app.scouting.trackr_live import fetch_programmes

    scraped = []
    if settings.trackr_live_enabled:
        try:
            scraped = fetch_programmes()
            results["live_scrape"] = len(scraped)
        except Exception:  # noqa: BLE001 - live scrape failing must not kill the sweep
            logger.exception("trackr live scrape failed; falling back to saved HTML")
            results["live_scrape"] = "failed"
    else:
        results["live_scrape"] = "disabled"

    # saved-HTML drops remain supported as an override/supplement
    scraped.extend(scrape_folder(trackr_folder(settings.data_dir)))
    results["parsed"] = len(scraped)
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
                from app.services.notifications import (
                    NotificationService,
                    queue_deadline_reminders,
                )

                results["deadline_reminders"] = queue_deadline_reminders(
                    session, NotificationService.from_settings(settings)
                )
            except Exception:  # noqa: BLE001 - reminders never break a sweep
                logger.exception("deadline reminder scan failed")
                results["deadline_reminders"] = "failed"
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
            outcome = run_sweep(app)
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
