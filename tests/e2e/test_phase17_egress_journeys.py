"""Phase 17 loopback browser proof; no external host or submit mode is used."""

from __future__ import annotations

import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app.automation.types import (
    RunMode,
    SessionCommand,
    SessionCommandType,
    SessionEventType,
    SessionState,
)
from app.services.navigator import HeadedSessionWorker


class _AssetHandler(BaseHTTPRequestHandler):
    hits = 0
    lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        with self.lock:
            type(self).hits += 1
        body = b"blocked asset must never arrive"
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


class _LabHandler(BaseHTTPRequestHandler):
    asset_origin = ""
    validation_hits = 0
    lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/validate":
            with self.lock:
                type(self).validation_hits += 1
            body = b'{"valid": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif path == "/tolerable":
            font_url = (
                "https://fonts.gstatic.com/s/roboto/v48/"
                "KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3yUBA.woff2"
            )
            body = f"""<!doctype html><html><head>
              <link rel="preload" href="{font_url}"
                    as="font" type="font/woff2" crossorigin>
              <style>@font-face {{ font-family: Phase17; src: url('{font_url}'); }} body {{ font-family: Phase17; }}</style>
              </head><body><form id="application-form">
              <label>First name<input id="first_name" name="first_name"></label>
              <label>Last name<input id="last_name" name="last_name"></label>
              </form></body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif path == "/fatal":
            body = b"""<!doctype html><html><body><form id="application-form">
              <label>First name<input id="first_name" name="first_name"></label>
              <label>Last name<input id="last_name" name="last_name"></label>
              <script>
                document.querySelector('#first_name').addEventListener('input', () => {
                  fetch('/validate?email=alex%40example.test').catch(() => {});
                });
              </script></form></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


@pytest.fixture()
def phase17_lab() -> tuple[str, type[_LabHandler], type[_AssetHandler]]:
    asset_server = ThreadingHTTPServer(("127.0.0.1", 0), _AssetHandler)
    lab_server = ThreadingHTTPServer(("127.0.0.1", 0), _LabHandler)
    _AssetHandler.hits = 0
    _LabHandler.validation_hits = 0
    _LabHandler.asset_origin = f"http://localhost:{asset_server.server_port}"
    asset_thread = threading.Thread(target=asset_server.serve_forever, daemon=True)
    lab_thread = threading.Thread(target=lab_server.serve_forever, daemon=True)
    asset_thread.start()
    lab_thread.start()
    try:
        yield (
            f"http://127.0.0.1:{lab_server.server_port}",
            _LabHandler,
            _AssetHandler,
        )
    finally:
        lab_server.shutdown()
        asset_server.shutdown()
        lab_server.server_close()
        asset_server.server_close()
        lab_thread.join(timeout=2)
        asset_thread.join(timeout=2)


class _LabJourney:
    def __init__(self, *, stop_on_fatal: bool) -> None:
        self.stop_on_fatal = stop_on_fatal
        self.fatal_probe = lambda: ""
        self.values: dict[str, str] = {}
        self.submit_calls = 0

    def set_egress_fatal_probe(self, probe) -> None:
        self.fatal_probe = probe

    def set_egress_fatal_records(self, records) -> None:
        pass

    def prepare(self, page) -> dict[str, object]:
        page.fill("#first_name", "Demo")
        page.wait_for_timeout(200)
        self.values["first_name"] = page.input_value("#first_name")
        if self.stop_on_fatal and self.fatal_probe():
            self.values["last_name"] = page.input_value("#last_name")
            return {
                "state": "NEEDS_USER",
                "reason": "Functional request was blocked during local prefill",
                "risk_level": 3,
                "blocked_reasons": ("egress_functional_or_unknown_blocked",),
                "manifest": {
                    "prefilled_fields": ["first_name"],
                    "submission": "not_clicked",
                },
            }
        page.fill("#last_name", "Candidate")
        page.wait_for_timeout(200)
        self.values["last_name"] = page.input_value("#last_name")
        return {
            "state": "NEEDS_USER",
            "reason": "Local prefill completed; human review remains",
            "risk_level": 3,
            "blocked_reasons": ("required_answer",),
            "manifest": {
                "prefilled_fields": ["first_name", "last_name"],
                "submission": "not_clicked",
            },
        }

    def submit(self, *_args: object, **_kwargs: object) -> None:
        self.submit_calls += 1
        raise AssertionError("Phase 17 must never call submit")


def _run_local_prefill(url: str, journey: _LabJourney) -> dict[str, object]:
    commands: queue.Queue = queue.Queue()
    events: queue.Queue = queue.Queue()
    worker = HeadedSessionWorker(
        session_id="phase17-loopback",
        application_id="phase17-loopback-application",
        mode=RunMode.PREFILL.value,
        url=url,
        summary={"provider": "greenhouse"},
        command_queue=commands,
        event_queue=events,
        ttl_seconds=20,
        headless=True,
        allowlist=frozenset({"127.0.0.1"}),
        journey_executor=journey,
        egress_impact_classification_enabled=True,
    )
    worker.start()
    deadline = time.monotonic() + 15
    result: dict[str, object] | None = None
    run_sent = False
    try:
        while time.monotonic() < deadline and result is None:
            event = events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event.event is SessionEventType.WORKER_READY and not run_sent:
                commands.put(
                    SessionCommand(
                        SessionCommandType.RUN_JOURNEY,
                        worker.session_id,
                    )
                )
                run_sent = True
            elif event.event is SessionEventType.JOURNEY_RESULT:
                result = dict(event.payload)
            elif event.event is SessionEventType.ERROR:
                raise AssertionError(event.reason)
        assert result is not None, "local Phase 17 journey did not emit a result"
        return result
    finally:
        commands.put(
            SessionCommand(
                SessionCommandType.CANCEL,
                worker.session_id,
                reason="phase17 loopback teardown",
            )
        )
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert worker._state in {SessionState.CANCELLED, SessionState.FAILED}


def test_blocked_third_party_font_still_completes_local_prefill(phase17_lab) -> None:
    lab_origin, _lab_handler, asset_handler = phase17_lab
    journey = _LabJourney(stop_on_fatal=False)

    result = _run_local_prefill(f"{lab_origin}/tolerable", journey)

    assert journey.values == {"first_name": "Demo", "last_name": "Candidate"}
    assert journey.submit_calls == 0
    assert asset_handler.hits == 0
    assert result["state"] == "NEEDS_USER"
    assert "filled; 1 non-essential resource blocked" in result["reason"]
    blocked = result["manifest"]["blocked_resources"]
    assert [(item["resource_type"], item["impact"]) for item in blocked] == [
        ("font", "tolerable")
    ]


def test_blocked_same_origin_xhr_stops_local_prefill(phase17_lab) -> None:
    lab_origin, lab_handler, _asset_handler = phase17_lab
    journey = _LabJourney(stop_on_fatal=True)

    result = _run_local_prefill(f"{lab_origin}/fatal", journey)

    assert journey.values == {"first_name": "Demo", "last_name": ""}
    assert journey.submit_calls == 0
    assert lab_handler.validation_hits == 0
    assert result["state"] == "NEEDS_USER"
    assert "functional or unclassified request" in result["reason"]
    blocked = result["manifest"]["blocked_resources"]
    assert [(item["resource_type"], item["impact"]) for item in blocked] == [
        ("fetch", "fatal")
    ]
