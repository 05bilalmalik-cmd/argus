"""Full-sweep endpoint: every discovery source -> ingest -> autopilot batch.

Extends /daily-sweep (saved Trackr HTML only) with live multi-source scraping.
Gated behind the same explicit opt-in as Trackr live scraping
(ARGUS_ENABLE_TRACKR_LIVE=true) because it makes outbound HTTP requests to
third-party sites. Read-only against those sites; never autofills anything.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.routers.scout import _shared_navigator
from app.routers.deps import SessionDep
from app.scouting.service import ScoutService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scout", tags=["scout"])


def _runner_factory(request: Request, settings):  # noqa: ANN001 - app state
    """Build a sweep runner bound to the create_app-owned Navigator."""

    def run(application_id: str, mode, headed: bool):  # noqa: ANN001
        from dataclasses import asdict

        from app.automation.runner import AutomationRunner
        from app.automation.types import RunMode

        outcome = AutomationRunner(
            request.app.state.db,
            settings,
            request.app.state.crypto,
            handoff_manager=_shared_navigator(request),
        ).run(application_id, RunMode(mode), headed=headed)
        payload = asdict(outcome)
        receipt = payload.get("receipt")
        payload["receipt"] = {k: v for k, v in receipt.items() if v} if receipt else None
        return payload

    return run


@router.post("/full-sweep")
def full_sweep(
    session: SessionDep,
    request: Request,
    max_runs: int = 10,
    include_trackr: bool = True,
) -> dict[str, object]:
    settings = getattr(request.app.state, "settings", None)
    if not bool(getattr(settings, "trackr_live_enabled", False)):
        raise HTTPException(
            status_code=403,
            detail=(
                "Full-sweep scraping is disabled. Set ARGUS_ENABLE_TRACKR_LIVE=true "
                "to opt in to outbound discovery requests."
            ),
        )
    from app.scouting.sources import collect_all
    from app.scouting.scheduler import trackr_folder
    from app.scouting.trackr import scrape_folder
    from app.scouting.trackr_live import fetch_programmes

    results: dict[str, object] = {}
    scraped = collect_all(settings)
    per_source: dict[str, int] = {}
    for item in scraped:
        key = str(getattr(item, "source", "unknown")).split(":", 1)[0]
        per_source[key] = per_source.get(key, 0) + 1
    results["per_source"] = per_source

    if include_trackr:
        if settings.trackr_live_enabled:
            try:
                scraped.extend(fetch_programmes())
            except Exception:  # noqa: BLE001 - one source failing must not kill sweep
                logger.exception("trackr live scrape failed during full-sweep")
        folder = trackr_folder(settings.data_dir)
        scraped.extend(scrape_folder(folder))

    results["total_scraped"] = len(scraped)

    scout = ScoutService(session, settings, request.app.state.crypto)
    results["ingest"] = scout.ingest(scraped)

    results["autopilot"] = scout.run_autopilot(
        _runner_factory(request, settings), max_runs=max_runs
    )
    try:
        from app.services.notifications import (
            NotificationService,
            queue_deadline_reminders,
        )

        results["deadline_reminders"] = queue_deadline_reminders(
            session, NotificationService.from_settings(settings)
        )
    except Exception:  # noqa: BLE001 - reminders never break a sweep
        logger.exception("deadline reminder scan failed during full-sweep")
        results["deadline_reminders"] = "failed"
    try:
        from datetime import datetime, timezone

        from app.scouting.scheduler import record_sweep_health

        results.setdefault("at", datetime.now(timezone.utc).isoformat())
        record_sweep_health(settings.data_dir, results)
    except Exception:  # noqa: BLE001 - health persistence must never break a sweep
        logger.warning("full-sweep health persist failed", exc_info=True)
    return results
