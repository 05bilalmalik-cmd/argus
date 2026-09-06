"""Decisive local-browser lifecycle coverage for Task 4."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.automation.types import RunMode, SessionState
from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.config import Settings
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, Opportunity
from app.services.navigator import ApplicationNavigator, DuplicateSessionError


class _LoopbackHandler(BaseHTTPRequestHandler):
    fetch_count = 0
    dynamic_stage = "before_fill"
    _lock = threading.Lock()

    def do_GET(self):  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path == "/stage":
            with self._lock:
                stage = type(self).dynamic_stage
            body = stage.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/set-stage":
            values = parse_qs(parsed.query).get("stage", [""])
            with self._lock:
                type(self).dynamic_stage = values[0]
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/loopback-data":
            with self._lock:
                type(self).fetch_count += 1
            body = b"loopback fetch accepted"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in {"/frame", "/popup"}:
            body = b"""
<!doctype html><html><body>
<div class="g-recaptcha" style="width:20px;height:20px"></div>
</body></html>
"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        dynamic = parsed.path == "/dynamic"
        multi = parsed.path == "/multi"
        body = (b"""
<!doctype html>
<html><body>
<button id="human-like" type="button">Continue</button>
<div id="result"></div>
__IFRAME__
<script>
  const dynamic = __DYNAMIC__;
  const multi = __MULTI__;
  async function pollStage() {
    if (!dynamic) return;
    const stage = await fetch('/stage').then(response => response.text());
    const old = document.querySelector('.g-recaptcha');
    if (old) old.remove();
    if (['before_fill', 'after_fill', 'after_next', 'before_final_activation'].includes(stage)) {
      const captcha = document.createElement('div');
      captcha.className = 'g-recaptcha';
      captcha.style.width = '20px'; captcha.style.height = '20px';
      captcha.dataset.stage = stage;
      document.body.appendChild(captcha);
    }
    setTimeout(pollStage, 50);
  }
  document.getElementById('human-like').addEventListener('click', async () => {
    const response = await fetch('/loopback-data');
    document.getElementById('result').textContent = await response.text();
  });
  // Synthetic human-like interaction happens after the HTTP start response;
  // no navigator command is needed for the routed fetch to complete.
  setTimeout(() => document.getElementById('human-like').click(), 100);
  if (!dynamic) setTimeout(() => {
    const captcha = document.createElement('div');
    captcha.className = 'g-recaptcha';
    captcha.style.width = '20px'; captcha.style.height = '20px';
    document.body.appendChild(captcha);
  }, 250);
  if (multi) setTimeout(() => window.open('/popup', 'argus-popup'), 120);
  pollStage();
</script>
</body></html>
""").replace(b"__DYNAMIC__", b"true" if dynamic else b"false").replace(
            b"__MULTI__", b"true" if multi else b"false"
        ).replace(
            b"__IFRAME__",
            b'<iframe src="/frame" id="captcha-frame"></iframe>' if multi else b"",
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture()
def loopback_server():
    _LoopbackHandler.fetch_count = 0
    _LoopbackHandler.dynamic_stage = "before_fill"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/application"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=6)


def _wait_for(predicate, timeout=15):
    """Poll until ``predicate`` holds.

    These tests drive a real browser, so every wait here is a real-time race.
    The original 2-5s budgets passed when this file ran alone and failed under
    a full-suite run, producing a different spurious failure each time; the
    budgets are tripled so a slow machine cannot masquerade as a regression.
    A genuine failure still fails - the predicate must become true either way.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _verified_resolution(url: str) -> TargetResolution:
    """Return the complete immutable target proof required by Navigator.start."""

    parts = urlsplit(url)
    path = parts.path or "/application"
    return TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="loopback",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": "loopback",
            "application_origin": origin_for_url(url),
            "employer": "Loopback Employer",
            "role": "Loopback Application",
            "requisition": path,
            "form_identity": path.rstrip("/").rsplit("/", 1)[-1] or "application",
        },
    )


def test_http_like_start_returns_before_human_click_and_routed_fetch(loopback_server, tmp_path):
    app = create_app(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    navigator = app.state.navigator
    navigator.ttl_seconds = 5
    navigator.headless = True
    navigator.url_resolver = lambda _application_id: (
        _verified_resolution(loopback_server),
        {"fixture": "loopback"},
    )
    try:
        with TestClient(app) as client:
            with client.app.state.db.session_scope() as session:
                opportunity = Opportunity(
                    employer="Loopback Employer",
                    role_title="Loopback Application",
                    cycle="2027",
                    url=f"{loopback_server}/source",
                    application_url=loopback_server,
                    target_status=TargetKind.APPLICATION_ENTRY.value,
                    resolved_ats_type="loopback",
                    resolution_evidence_json=json.dumps(
                        {
                            "source_url": f"{loopback_server}/source",
                            "final_url": loopback_server,
                            "kind": TargetKind.APPLICATION_ENTRY.value,
                            "provider": "loopback",
                            "identity_verified": True,
                            "form_verified": False,
                            "reason_codes": [],
                            "evidence": {
                                "synthetic_lab": True,
                                "provider": "loopback",
                                "application_origin": origin_for_url(loopback_server),
                                "employer": "Loopback Employer",
                                "role": "Loopback Application",
                                "requisition": urlsplit(loopback_server).path,
                                "form_identity": "application",
                            },
                        }
                    ),
                    resolved_at=datetime.now(timezone.utc),
                    source="integration-test",
                )
                session.add(opportunity)
                session.flush()
                application = Application(
                    opportunity_id=opportunity.id,
                    state=ApplicationState.NEEDS_USER.value,
                )
                session.add(application)
                session.flush()
                application_id = application.id
            started = time.monotonic()
            response = client.post(
                f"/api/handoff/applications/{application_id}/start",
                params={"mode": RunMode.REVIEW.value},
            )
            assert time.monotonic() - started < 1
            assert response.status_code == 202
            payload = response.json()
            session_id = payload["session_id"]
            assert payload["status"] == "OPENING"
            active = navigator.wait_for_state(session_id, SessionState.ACTIVE, timeout=15)
            assert active.state is SessionState.ACTIVE, active.reason

            assert _wait_for(lambda: _LoopbackHandler.fetch_count == 1, timeout=15)
            # The browser stayed live and processed the click/fetch while no
            # continue/click command was sent through the manager.
            assert navigator.get(session_id).worker_alive
            diagnostics = navigator.diagnostics(session_id)
            assert diagnostics.owner_thread_id
            assert diagnostics.operation_log
            assert all(
                thread_id == diagnostics.owner_thread_id
                for _name, thread_id in diagnostics.operation_log
            )

            navigator.cancel(session_id, reason="test teardown")
            terminal = navigator.wait_for_terminal(session_id, timeout=15)
            assert terminal.state is SessionState.CANCELLED
            assert not terminal.worker_alive
            final_diagnostics = navigator.diagnostics(session_id)
            assert final_diagnostics.teardown_complete is True
            assert not final_diagnostics.teardown_failures
            assert (
                final_diagnostics.owner_thread_id
                == diagnostics.owner_thread_id
            )
    finally:
        navigator.shutdown()
        navigator.shutdown()


def test_real_worker_keeps_same_context_for_captcha_and_requires_continue(loopback_server):
    navigator = ApplicationNavigator(
        ttl_seconds=5,
        headless=True,
        url_resolver=lambda _application_id: (
            _verified_resolution(loopback_server),
            {"fixture": "captcha"},
        ),
    )
    try:
        session = navigator.start("application-2", RunMode.REVIEW)
        navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=15)
        human_required = navigator.wait_for_state(
            session.session_id, SessionState.HUMAN_REQUIRED, timeout=15
        )
        assert human_required.state is SessionState.HUMAN_REQUIRED
        page_identity = navigator.diagnostics(session.session_id).page_token
        navigator.continue_after_human(session.session_id)
        # CAPTCHA remains in the fixture, so Continue cannot silently turn it
        # into a final/confirmed state or create a replacement context.
        assert _wait_for(
            lambda: navigator.get(session.session_id).state is SessionState.HUMAN_REQUIRED,
            timeout=6,
        )
        assert navigator.diagnostics(session.session_id).page_token == page_identity
    finally:
        navigator.cancel(session.session_id, reason="test teardown")
        navigator.shutdown()


def test_duplicate_application_is_refused_until_owner_teardown(loopback_server):
    navigator = ApplicationNavigator(
        ttl_seconds=5,
        headless=True,
        url_resolver=lambda _application_id: (_verified_resolution(loopback_server), {}),
    )
    try:
        first = navigator.start("application-3", RunMode.REVIEW)
        with pytest.raises(DuplicateSessionError):
            navigator.start("application-3", RunMode.REVIEW)
        navigator.close(first.session_id)
        first_closed = navigator.wait_for_cleanup(first.session_id, timeout=15)
        assert first_closed.cleanup_complete is True
        assert not first_closed.worker_alive
        second = navigator.start("application-3", RunMode.REVIEW)
        navigator.cancel(second.session_id)
        second_closed = navigator.wait_for_cleanup(second.session_id, timeout=15)
        assert second_closed.cleanup_complete is True
        assert not second_closed.worker_alive
    finally:
        navigator.shutdown()


def test_dynamic_captcha_boundaries_require_continue_and_keep_context(loopback_server):
    dynamic_url = loopback_server.replace("/application", "/dynamic")
    navigator = ApplicationNavigator(
        ttl_seconds=10,
        headless=True,
        url_resolver=lambda _application_id: (
            _verified_resolution(dynamic_url),
            {"fixture": "dynamic-captcha"},
        ),
    )
    session = None
    try:
        session = navigator.start("application-4", RunMode.REVIEW)
        navigator.wait_for_state(session.session_id, SessionState.HUMAN_REQUIRED, timeout=15)
        page_identity = navigator.diagnostics(session.session_id).page_token
        for stage in ("after_fill", "after_next", "before_final_activation"):
            # The synthetic human resolves the current boundary in the same
            # browser context; Continue is still an explicit ARGUS command.
            httpx.get(
                loopback_server.replace("/application", f"/set-stage?stage=clear"),
                timeout=6,
            )
            before_scans = sum(
                1
                for name, _thread in navigator.diagnostics(session.session_id).operation_log
                if name.endswith("human_boundary")
            )
            assert _wait_for(
                lambda: sum(
                    1
                    for name, _thread in navigator.diagnostics(session.session_id).operation_log
                    if name.endswith("human_boundary")
                )
                > before_scans,
                timeout=9,
            )
            navigator.continue_after_human(session.session_id)
            assert _wait_for(
                lambda: navigator.get(session.session_id).state is SessionState.ACTIVE,
                timeout=9,
            )
            httpx.get(
                loopback_server.replace("/application", f"/set-stage?stage={stage}"),
                timeout=6,
            )
            assert _wait_for(
                lambda: navigator.get(session.session_id).state is SessionState.HUMAN_REQUIRED,
                timeout=9,
            )
            assert navigator.diagnostics(session.session_id).page_token == page_identity
        navigator.request_final_manifest(session.session_id)
        assert navigator.get(session.session_id).state is SessionState.HUMAN_REQUIRED
    finally:
        if session is not None:
            navigator.cancel(session.session_id, reason="test teardown")
        navigator.shutdown()


def test_captcha_disappearance_does_not_clear_human_required_without_continue(loopback_server):
    dynamic_url = loopback_server.replace("/application", "/dynamic")
    navigator = ApplicationNavigator(
        ttl_seconds=10,
        headless=True,
        url_resolver=lambda _application_id: (
            _verified_resolution(dynamic_url),
            {"fixture": "captcha-persistence"},
        ),
    )
    session = None
    try:
        session = navigator.start("application-5", RunMode.REVIEW)
        assert navigator.wait_for_state(
            session.session_id,
            SessionState.HUMAN_REQUIRED,
            timeout=15,
        ).state is SessionState.HUMAN_REQUIRED
        httpx.get(
            loopback_server.replace("/application", "/set-stage?stage=clear"),
            timeout=6,
        )
        before_scans = sum(
            1
            for name, _thread in navigator.diagnostics(session.session_id).operation_log
            if name.endswith("human_boundary")
        )
        assert _wait_for(
            lambda: sum(
                1
                for name, _thread in navigator.diagnostics(session.session_id).operation_log
                if name.endswith("human_boundary")
            )
            > before_scans,
            timeout=9,
        )
        assert navigator.get(session.session_id).state is SessionState.HUMAN_REQUIRED
        navigator.continue_after_human(session.session_id)
        assert navigator.wait_for_state(
            session.session_id,
            SessionState.ACTIVE,
            timeout=9,
        ).state is SessionState.ACTIVE
    finally:
        if session is not None:
            navigator.cancel(session.session_id, reason="test teardown")
        navigator.shutdown()


def test_context_routes_and_scans_loopback_iframe_and_popup(loopback_server):
    multi_url = loopback_server.replace("/application", "/multi")
    navigator = ApplicationNavigator(
        ttl_seconds=10,
        headless=True,
        url_resolver=lambda _application_id: (
            _verified_resolution(multi_url),
            {"fixture": "iframe-popup"},
        ),
    )
    session = None
    try:
        session = navigator.start("application-6", RunMode.REVIEW)
        assert navigator.wait_for_state(
            session.session_id,
            SessionState.HUMAN_REQUIRED,
            timeout=15,
        ).state is SessionState.HUMAN_REQUIRED
        diagnostics = navigator.diagnostics(session.session_id)
        assert any(name == "context.route" for name, _thread in diagnostics.operation_log)
        assert any("frame.evaluate" in name for name, _thread in diagnostics.operation_log)
        assert any("popup" in name or name == "context.page" for name, _thread in diagnostics.operation_log)
        assert all(
            thread_id == diagnostics.owner_thread_id
            for _name, thread_id in diagnostics.operation_log
        )
    finally:
        if session is not None:
            navigator.cancel(session.session_id, reason="test teardown")
        navigator.shutdown()
