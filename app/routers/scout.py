"""Scouting + autopilot API endpoints."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.automation.runner import AutomationRunner
from app.routers.deps import SessionDep
from app.scouting.service import ScoutService
from app.scouting.trackr import scrape_folder, scrape_trackr_file

router = APIRouter(prefix="/api/scout", tags=["scout"])


def _shared_navigator(request: Request):  # noqa: ANN001 - FastAPI app state
    """Return the one navigator owned by ``create_app``.

    Scout/autopilot runs may pause for a CAPTCHA, MFA challenge, declaration,
    or another human-only boundary.  Those pauses must remain visible to the
    handoff routes, so these entry points must never create or silently fall
    back to a second handoff manager.  A missing navigator is a wiring error;
    fail closed instead of running automation with an unobservable session.
    """

    try:
        navigator = request.app.state.navigator
    except AttributeError as exc:
        raise RuntimeError(
            "ARGUS application navigator is not installed on app.state"
        ) from exc
    if navigator is None:
        raise RuntimeError("ARGUS application navigator is unavailable")
    return navigator


def _scout(session: Session, request: Request) -> ScoutService:
    return ScoutService(session, request.app.state.settings, request.app.state.crypto)


def _require_trackr_live(request: Request) -> None:
    """Require an explicit opt-in before making any live Trackr request."""

    settings = getattr(request.app.state, "settings", None)
    if not bool(getattr(settings, "trackr_live_enabled", False)):
        raise HTTPException(
            status_code=403,
            detail=(
                "Live Trackr scraping is disabled. Set "
                "ARGUS_ENABLE_TRACKR_LIVE=true to opt in."
            ),
        )


def _runner_factory(request: Request):
    def run(application_id: str, mode, headed: bool) -> dict[str, object]:
        runner = AutomationRunner(
            request.app.state.db,
            request.app.state.settings,
            request.app.state.crypto,
            handoff_manager=_shared_navigator(request),
        )
        try:
            return _outcome_dict(
                runner.run(application_id, mode, headed=headed)
            )
        except Exception:
            raise

    return run


def _outcome_dict(outcome) -> dict[str, object]:  # noqa: ANN001
    from dataclasses import asdict, is_dataclass

    if is_dataclass(outcome):
        payload = asdict(outcome)
        receipt = payload.get("receipt")
        if receipt is not None:
            payload["receipt"] = {k: v for k, v in receipt.items() if v}
        else:
            payload["receipt"] = None
        return payload
    return dict(outcome)


@router.post("/upload-trackr-html")
async def upload_trackr_html(file: UploadFile, session: SessionDep, request: Request) -> dict[str, object]:  # noqa: B008
    """Upload a saved Trackr page (Ctrl+S -> the .html). Parsed + ingested."""
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "Trackr HTML too large (20 MiB max)")
    html = raw.decode("utf-8", errors="replace")
    scraped = scrape_trackr_file(html, source_label=file.filename or "trackr.html")
    stats = _scout(session, request).ingest(scraped)
    return {"parsed": len(scraped), **stats}


@router.post("/scan-folder")
def scan_folder(session: SessionDep, request: Request) -> dict[str, object]:
    """Scan the watched Trackr saves folder for new/changed HTML files."""
    folder = Path(request.app.state.settings.data_dir) / "trackr_saves"
    folder.mkdir(parents=True, exist_ok=True)
    scraped = scrape_folder(folder)
    stats = _scout(session, request).ingest(scraped)
    return {
        "folder": str(folder),
        "parsed": len(scraped),
        **stats,
        "hint": "Drop saved Trackr pages (.html) into this folder, then press Scan.",
    }


@router.post("/scrape-live")
def scrape_live(session: SessionDep, request: Request) -> dict[str, object]:
    """Scrape Trackr's public trackers right now and ingest everything found."""
    _require_trackr_live(request)
    from app.scouting.trackr_live import fetch_programmes

    scraped = fetch_programmes()
    with_url = sum(1 for s in scraped if s.url)
    stats = _scout(session, request).ingest(scraped)
    return {"live_scrape": len(scraped), "with_application_url": with_url, **stats}


@router.get("/queue")
def scout_queue(session: SessionDep, request: Request) -> list[dict[str, object]]:
    """What the autopilot would process right now, in priority order."""
    service = _scout(session, request)
    out: list[dict[str, object]] = []
    for application, opportunity in service.autopilot_candidates():
        out.append(
            {
                "application_id": application.id,
                "priority": application.priority,
                "state": application.state,
                "employer": opportunity.employer,
                "role": opportunity.role_title,
                "programme_group": opportunity.programme_group,
                "deadline": opportunity.deadline.isoformat() if opportunity.deadline else None,
                "url": opportunity.url,
            }
        )
    return out


def _batch_submit_env_enabled() -> bool:
    """Removed: batch submission was retired (forensic root cause 7).

    A single boolean could once confirm every ready application at once.
    Submission now requires exact per-application, action-time confirmation
    via /api/applications/{id}/run?mode=submit with
    ``{"application_id": "<exact-id>"}`` in the JSON body.
    """

    return False


@router.post("/autopilot")
def autopilot(
    session: SessionDep,
    request: Request,
    max_runs: int = 10,
    headed: bool = False,
) -> dict[str, object]:
    """Run the full pipeline over every auto-eligible application.

    Human-required steps (CAPTCHA, declarations, assessments, demographics)
    stop that application cleanly - it lands on the Needs You page.

    There is deliberately NO batch submission switch. Applications verified
    ready stop at ``awaiting_action_time_confirmation`` and can proceed only
    through an exact per-application confirmed submit request.
    """
    scout = _scout(session, request)
    result = scout.run_autopilot(
        _runner_factory(request), max_runs=max_runs, headed=headed
    )
    return result


@router.post("/daily-sweep")
def daily_sweep(session: SessionDep, request: Request) -> dict[str, object]:
    """One-click morning run: scan folder -> ingest -> autopilot."""
    folder = Path(request.app.state.settings.data_dir) / "trackr_saves"
    folder.mkdir(parents=True, exist_ok=True)
    scraped = scrape_folder(folder)
    scout = _scout(session, request)
    ingest_stats = scout.ingest(scraped)
    autopilot_stats = scout.run_autopilot(_runner_factory(request))
    return {"ingest": ingest_stats, "autopilot": autopilot_stats}
