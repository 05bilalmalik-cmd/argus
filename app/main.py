from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.db import configure_database
from app.routers import api, decisions, handoff, lab, mail, pages, scout, scout_pages, sweep, review_queue
from app.routers import review_pages
from app.security.crypto import CryptoBox
from app.security.http import LocalSecurityMiddleware
from app.services.navigator import ApplicationNavigator, bind_service_navigator
from app.services.notifications import (
    NavigatorNotificationMonitor,
    NotificationService,
)
from app.services.target_resolution import NavigatorTargetResolver
from app.version import __version__


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.load()
    resolved.ensure_directories()
    database = configure_database(resolved)
    crypto = CryptoBox.from_path(resolved.secret_key_path)
    notifier = NotificationService.from_settings(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.create_schema()
        from app.scouting.scheduler import start_scheduler

        start_scheduler(app, resolved.sweep_interval_hours)
        app.state.notification_monitor.start()
        try:
            yield
        finally:
            # Browser ownership is deterministic at application shutdown:
            # commands are queued and each worker closes its own Playwright
            # page/context/browser/runtime before the scheduler is stopped.
            try:
                app.state.notification_monitor.stop()
            finally:
                try:
                    app.state.navigator.shutdown()
                finally:
                    from app.scouting.scheduler import stop_scheduler

                    try:
                        stop_scheduler()
                    finally:
                        notifier.flush()

    app = FastAPI(
        title="ARGUS",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.db = database
    app.state.crypto = crypto
    app.state.notifier = notifier
    app.state.lab_submissions = []
    app.state.navigator = ApplicationNavigator(database, resolved)
    bind_service_navigator(database, app.state.navigator)
    app.state.notification_monitor = NavigatorNotificationMonitor(
        app.state.navigator,
        database,
        notifier,
        enabled=notifier.enabled,
    )
    # Keep the legacy state key for runner/API callers while routing all new
    # handoff operations through the owner-thread navigator.
    app.state.handoff_manager = app.state.navigator
    # Source-target resolution is application-owned.  The resolver receives
    # only the exact application id from the API and delegates to the
    # Navigator's headed source-resolution hook; it never accepts a caller
    # supplied destination URL.  Until a verified result exists, the service
    # records a persistent human handoff rather than promoting the source.
    app.state.target_resolution_resolver = NavigatorTargetResolver(
        app.state.navigator
    )
    app.add_middleware(LocalSecurityMiddleware)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(api.router)
    app.include_router(decisions.router)
    app.include_router(mail.router)
    app.include_router(lab.router)
    app.include_router(pages.router)
    app.include_router(scout.router)
    app.include_router(scout_pages.router)
    app.include_router(sweep.router)
    app.include_router(handoff.router)
    app.include_router(review_queue.router)
    app.include_router(review_pages.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        try:
            from app.scouting.scheduler import read_sweep_health

            sweep = read_sweep_health(resolved.data_dir)
        except Exception:  # noqa: BLE001 - healthz must never fail on stale state
            sweep = {}
        if isinstance(sweep, dict) and sweep.get("at"):
            if sweep.get("error"):
                sweep_status = "error"
            elif sweep.get("skipped"):
                sweep_status = "skipped"
            else:
                sweep_status = "ok"
        else:
            sweep_status = "unknown"
        return {
            "status": "ok",
            "service": "ARGUS",
            "version": __version__,
            "automation_mode": resolved.automation_mode.value,
            "live_submit": resolved.live_submit_enabled,
            "trackr_live": resolved.trackr_live_enabled,
            "sweep_last_at": sweep.get("at") if isinstance(sweep, dict) else None,
            "sweep_status": sweep_status,
            "sweep_live_scrape": sweep.get("live_scrape")
            if isinstance(sweep, dict)
            else None,
        }

    return app


app = create_app()
