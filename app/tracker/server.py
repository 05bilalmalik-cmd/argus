"""Loopback-only tracker UI/API, independently runnable from legacy ARGUS."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.datastructures import Headers
from starlette.types import Receive, Scope, Send

from app.security.http import LocalSecurityMiddleware
from app.tracker.contracts import SourceResult
from app.tracker.runtime import RefreshCoordinator
from app.tracker.views import decorate_job, export_csv, filter_jobs


class JobUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    saved: StrictBool | None = None
    stage: str | None = None
    notes: str | None = None
    due_date: str | None = None


def _mutation_header(request: Request) -> None:
    if request.headers.get('X-Argus-Tracker') != '1':
        raise HTTPException(403, 'Tracker request header required')


class _ForwardedLoopbackPortNormalization:
    """Align the ASGI server port with the loopback Host header port.

    The remote UI opens the tracker through an SSH tunnel that forwards
    local ``127.0.0.1:8792`` to the server's ``127.0.0.1:8791`` (see
    ``app/tracker/remote_ui.py``). Browsers then send ``Host``/``Origin``
    with port 8792 while the server socket reports 8791. The shared
    ``LocalSecurityMiddleware`` compares ``Origin`` against the socket port,
    which would wrongly reject that legitimate same-origin request.

    This middleware runs ahead of the security check and, when the ``Host``
    header carries an explicit numeric port, presents that port as the scope
    server port so the downstream check becomes a correct Origin-vs-Host
    comparison. It performs no allow decision itself: Host-trust (400)
    and cross-site (403) enforcement still run unchanged downstream. A
    ``Host`` header with an unparseable port is rejected here with the same
    400 ``Untrusted Host header`` body the shared middleware uses, because
    the shared check only inspects the hostname and would otherwise let a
    malformed port fall through to a 403 Origin refusal.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get('type') == 'http':
            try:
                host_header = Headers(scope=scope).get('host', '')
                host_port = urlsplit(f'//{host_header}').port
            except ValueError:
                response = JSONResponse({'detail': 'Untrusted Host header'}, status_code=400)
                await response(scope, receive, send)
                return
            if host_port is not None:
                server = scope.get('server')
                if server and len(server) == 2:
                    scope = {**scope, 'server': (server[0], host_port)}
                else:
                    scope = {**scope, 'server': ('127.0.0.1', host_port)}
        await self.app(scope, receive, send)


def create_tracker_app(data_dir: Path, *, refresh_minutes: float = 30,
                       collector: Callable[[], list[SourceResult]] | None = None,
                       auto_refresh: bool = True, automated: bool = False,
                       intelligence=None, alerts=None) -> FastAPI:
    from app.tracker.store import TrackerStore

    if collector is None:
        from app.tracker.sources import collect_all_sources
        collector = collect_all_sources
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = TrackerStore(root / 'tracker.sqlite3')
    pipeline = None
    if automated:
        from app.tracker.pipeline import AutomationPipeline
        if intelligence is None:
            from app.tracker.intelligence import EnrichmentService
            intelligence = EnrichmentService(root)
        if alerts is None:
            from app.tracker.mail_settings import ConfiguredAlerts
            alerts = ConfiguredAlerts(root)
        pipeline = AutomationPipeline(root, store, collector, intelligence, alerts)
    refresh = RefreshCoordinator(root, store, collector, interval_seconds=refresh_minutes * 60,
                                 executor=pipeline.run if pipeline else None)

    def decorated_rows():
        rows = store.list_jobs()
        return intelligence.decorate(rows) if intelligence else rows

    @asynccontextmanager
    async def lifespan(_app):
        if auto_refresh:
            refresh.start()
        try:
            yield
        finally:
            refresh.stop()

    app = FastAPI(title='ARGUS Internship Tracker', lifespan=lifespan,
                  docs_url='/api/docs', redoc_url=None)
    app.state.store, app.state.refresh = store, refresh
    app.state.pipeline, app.state.intelligence, app.state.alerts = pipeline, intelligence, alerts
    app.add_middleware(LocalSecurityMiddleware)
    # Added last so it runs first (Starlette builds the stack in reverse):
    # normalize the forwarded port before the security check sees the scope.
    app.add_middleware(_ForwardedLoopbackPortNormalization)
    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static, check_dir=False), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI otherwise echoes invalid request bodies, including passwords.
        return JSONResponse({'detail': [{key: error[key] for key in ('loc', 'msg', 'type')}
                                        for error in exc.errors()]}, status_code=422)

    @app.get('/settings/email')
    def email_setup():
        return FileResponse(static / 'email-setup.html')

    def configured_mail():
        if alerts is None or not hasattr(alerts, 'configure'):
            raise HTTPException(409, 'Interactive mail setup is unavailable in this runtime')
        return alerts

    @app.get('/api/email/configuration')
    def email_configuration():
        return configured_mail().configuration_status()

    @app.put('/api/email/configuration', dependencies=[Depends(_mutation_header)])
    def configure_email(payload: dict):
        try:
            return configured_mail().configure(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/api/email/test', dependencies=[Depends(_mutation_header)])
    def test_email():
        try:
            return configured_mail().send_test()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/favicon.ico')
    def favicon():
        return Response(status_code=204)

    @app.get('/healthz')
    def health():
        return {'status': 'ok', 'service': 'ARGUS Tracker', 'live_submit': False,
                'application_automation': False, 'version': 'tracker-2',
                'automated_tracker': automated, 'refresh': refresh.snapshot()}

    @app.get('/api/status')
    def status():
        sources = store.list_sources()
        summary = store.summary()
        summary['source_attention'] = sum(item['status'] in {'partial', 'error'} for item in sources)
        rows = decorated_rows()
        summary.update(
            never_checked=not bool(sources),
            verified_open=sum(row.get('availability') == 'open' for row in rows),
            usable_deadlines=sum(bool(row.get('deadline')) for row in rows),
            potential_matches=sum(row.get('match_status') == 'potential' for row in rows),
        )
        return {'summary': summary, 'refresh': refresh.snapshot(), 'sources': sources,
                'pipeline': pipeline.snapshot() if pipeline else None,
                'intelligence': intelligence.summary() if intelligence else {'status': 'disabled'},
                'alerts': alerts.summary() if alerts else {'status': 'disabled'}}

    @app.get('/readyz')
    def ready():
        stages = pipeline.snapshot()['stages'] if pipeline else {}
        complete = bool(stages) and all(s['status'] == 'ok' for s in stages.values())
        return Response(json.dumps({
            'status': 'ready' if complete else 'degraded',
            'stages': {name: value['status'] for name, value in stages.items()},
        }), status_code=200 if complete else 503, media_type='application/json')

    @app.get('/api/profile')
    def get_profile():
        if intelligence is None:
            raise HTTPException(409, 'Employer intelligence is disabled')
        return {'profile': intelligence.get_profile()}

    @app.put('/api/profile', dependencies=[Depends(_mutation_header)])
    def set_profile(payload: dict):
        if intelligence is None:
            raise HTTPException(409, 'Employer intelligence is disabled')
        try:
            return {'profile': intelligence.set_profile(payload)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/alerts')
    def alert_history(limit: int = Query(50, ge=1, le=200)):
        return {'events': alerts.list_events(limit=limit) if alerts else [],
                'summary': alerts.summary() if alerts else {'status': 'disabled'}}

    @app.get('/api/jobs/{job_id}')
    def job_detail(job_id: int):
        for row in decorated_rows():
            if row['id'] == job_id:
                return decorate_job(row)
        raise HTTPException(404, 'Role not found')

    def selection(q: str = '', programmes: str = '', uk_only: bool = True,
                  saved_only: bool = False, new_only: bool = False,
                  deadline_soon: bool = False, include_expired: bool = False,
                  stage: str = '', source: str = '', sort: str = 'newest',
                  match_status: str = '', availability: str = ''):
        try:
            return filter_jobs(decorated_rows(), q=q, programmes=programmes, uk_only=uk_only,
                               saved_only=saved_only, new_only=new_only, deadline_soon=deadline_soon,
                               include_expired=include_expired, stage=stage, source=source, sort=sort,
                               match_status=match_status, availability=availability)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/jobs')
    def jobs(rows: list = Depends(selection), limit: int = Query(100, ge=1, le=500),
             offset: int = Query(0, ge=0)):
        return {'jobs': rows[offset:offset + limit], 'total': len(rows),
                'offset': offset, 'limit': limit, 'has_more': offset + limit < len(rows)}

    @app.get('/api/export.csv')
    def export(rows: list = Depends(selection)):
        return Response(export_csv(rows), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="argus-internships.csv"'})

    @app.patch('/api/jobs/{job_id}', dependencies=[Depends(_mutation_header)])
    def update(job_id: str, payload: JobUpdate):
        try:
            return store.update_job(job_id, **payload.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(404, 'Role not found') from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/api/refresh', dependencies=[Depends(_mutation_header)], status_code=202)
    def trigger_refresh():
        if not refresh.request_refresh():
            raise HTTPException(409, 'Refresh already running or tracker stopping')
        return {'accepted': True, 'refresh': refresh.snapshot()}

    return app
