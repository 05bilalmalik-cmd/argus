"""Real-browser loopback coverage for the truthful Apply/Navigator UI.

The fixture replaces only the local Navigator service with a typed in-process
fake.  All browser traffic still goes through a loopback Uvicorn server and
the production templates, static JavaScript, CSP middleware, and handoff
routes.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.automation.types import RunMode, SessionSnapshot, SessionState
from app.config import Settings
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, Opportunity
from app.services.navigator import DuplicateSessionError, SessionNotFoundError


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _FakeSession:
    session_id: str
    application_id: str
    state: SessionState = SessionState.OPENING
    reason: str = ""
    manifest: dict[str, object] = field(default_factory=dict)
    mode: str = RunMode.REVIEW.value
    continue_count: int = 0
    cancel_count: int = 0
    confirm_count: int = 0


class _LoopbackNavigator:
    """Small typed session fake; it never exposes browser objects."""

    def __init__(self, scripts: dict[str, dict[str, object]]) -> None:
        self.scripts = scripts
        self.sessions: dict[str, _FakeSession] = {}
        self.starts: list[str] = []
        self.next_session = 0
        self.shutdown_called = False

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _snapshot(self, session: _FakeSession) -> SessionSnapshot:
        now = self._now()
        expires_at = now + timedelta(minutes=5)
        human_boundary = {}
        if session.state is SessionState.HUMAN_REQUIRED:
            human_boundary = {
                "session_id": session.session_id,
                "application_id": session.application_id,
                "kind": "human_review",
                "reason": session.reason or "human action required",
                "expires_at": expires_at.isoformat(),
                "can_continue": True,
                "can_cancel": True,
                "resumable": True,
                "no_auto_submit": True,
            }
        return SessionSnapshot(
            session_id=session.session_id,
            application_id=session.application_id,
            mode=session.mode,
            state=session.state,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            reason=session.reason,
            summary={
                "employer": self.scripts.get(session.application_id, {}).get("employer", ""),
                "role": self.scripts.get(session.application_id, {}).get("role", ""),
                "provider": self.scripts.get(session.application_id, {}).get("provider", ""),
                "destination": self.scripts.get(session.application_id, {}).get("destination", ""),
            },
            manifest=session.manifest,
            owner_thread_id=1234,
            worker_alive=session.state in {
                SessionState.OPENING,
                SessionState.ACTIVE,
                SessionState.HUMAN_REQUIRED,
                SessionState.FINAL_REVIEW,
            },
            cleanup_complete=session.state.terminal,
            human_boundary=human_boundary,
        )

    def start(self, application_id: str, mode: RunMode | str) -> SessionSnapshot:
        existing = next(
            (
                session
                for session in self.sessions.values()
                if session.application_id == application_id and not session.state.terminal
            ),
            None,
        )
        if existing is not None:
            raise DuplicateSessionError("application already has an active session")
        script = self.scripts.get(application_id, {})
        if script.get("start_error"):
            raise DuplicateSessionError(str(script["start_error"]))
        self.next_session += 1
        session_id = f"session-{self.next_session}"
        session = _FakeSession(
            session_id=session_id,
            application_id=application_id,
            mode=mode.value if isinstance(mode, RunMode) else str(mode),
            state=SessionState.OPENING,
            reason="Navigator opening verified target",
        )
        self.sessions[session_id] = session
        self.starts.append(application_id)
        return self._snapshot(session)

    def get(self, session_id: str) -> SessionSnapshot:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        script = self.scripts.get(session.application_id, {})
        if session.state is SessionState.OPENING:
            target = str(script.get("state", "ACTIVE"))
            session.state = SessionState(target)
            session.reason = str(script.get("reason", "Navigator is active"))
            manifest = script.get("manifest")
            if isinstance(manifest, dict):
                session.manifest = dict(manifest)
        return self._snapshot(session)

    def all_sessions(self) -> list[SessionSnapshot]:
        return [self._snapshot(session) for session in self.sessions.values()]

    def continue_after_human(self, session_id: str) -> SessionSnapshot:
        session = self.sessions[session_id]
        session.continue_count += 1
        target = self.scripts.get(session.application_id, {}).get("after_continue", "ACTIVE")
        session.state = SessionState(str(target))
        session.reason = "Human boundary cleared; Navigator resumed. No submit was issued."
        return self._snapshot(session)

    def cancel(self, session_id: str, *, reason: str = "") -> SessionSnapshot:
        session = self.sessions[session_id]
        session.cancel_count += 1
        session.state = SessionState.CANCELLED
        session.reason = reason or "Cancelled by user"
        return self._snapshot(session)

    close = cancel

    def request_final_manifest(self, session_id: str) -> SessionSnapshot:
        session = self.sessions[session_id]
        script = self.scripts.get(session.application_id, {})
        session.state = SessionState.FINAL_REVIEW
        session.reason = "Exact manifest ready for one-time confirmation"
        session.manifest = dict(script.get("manifest", {}))
        return self._snapshot(session)

    def confirm(self, session_id: str) -> SessionSnapshot:
        session = self.sessions[session_id]
        session.confirm_count += 1
        session.state = SessionState.CONFIRMED
        session.reason = "Manifest confirmed; no submit click was issued"
        return self._snapshot(session)

    def shutdown(self) -> None:
        self.shutdown_called = True


@dataclass
class _BrowserServer:
    app: Any
    base_url: str
    navigator: _LoopbackNavigator
    server: uvicorn.Server
    thread: threading.Thread


def _wait_ready(base_url: str) -> None:
    import httpx

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=0.25)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise AssertionError("loopback ARGUS server did not become ready")


def _seed(
    app: Any,
    *,
    employer: str,
    state: str = "NEEDS_USER",
    target_status: str = TargetKind.APPLICATION_ENTRY.value,
    application_url: str | None = "http://127.0.0.1:1/lab/ats/standard",
    provider: str = "greenhouse",
    reason_codes: tuple[str, ...] = (),
) -> tuple[str, str]:
    source_url = f"http://127.0.0.1:1/source/{employer.casefold().replace(' ', '-')}"
    resolution_evidence: dict[str, object] = {"reason_codes": list(reason_codes)}
    resolved_at = None
    if application_url and target_status in {
        TargetKind.APPLICATION_ENTRY.value,
        TargetKind.APPLICATION_FORM.value,
    }:
        form_identity = "fixture-form-v1"
        resolution_evidence = {
            "source_url": source_url,
            "final_url": application_url,
            "kind": target_status,
            "provider": provider,
            "identity_verified": True,
            "form_verified": target_status == TargetKind.APPLICATION_FORM.value,
            "reason_codes": list(reason_codes),
            "evidence": {
                "provider": provider,
                "employer": employer,
                "role": f"{employer} Summer Analyst",
                "requisition": "/lab/ats/standard",
                "form_identity": form_identity,
                "frame_url": application_url,
                "application_origin": application_url,
                "binding": {
                    "binding_verified": True,
                    "bound_target_url": application_url,
                    "bound_provider": provider,
                    "bound_root_token": "fixture-root-v1",
                    "bound_form_identity": form_identity,
                    "requisition": "/lab/ats/standard",
                },
            },
        }
        resolved_at = datetime.now(timezone.utc)
    with app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=f"{employer} Summer Analyst",
            division="Private Credit",
            programme_group="summer",
            location="London",
            cycle="2027",
            url=source_url,
            application_url=application_url,
            target_status=target_status,
            resolved_ats_type=provider if application_url else "",
            resolution_evidence_json=json.dumps(resolution_evidence),
            resolved_at=resolved_at,
            source="loopback",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=state,
            priority=80,
            risk_level=0,
            next_action="Review the Navigator state",
        )
        session.add(application)
        session.flush()
        return application.id, opportunity.id


def _exact_manifest(application_id: str, employer: str, role: str) -> dict[str, object]:
    """Return the complete load-bearing manifest used by browser fixtures."""

    application_url = "http://127.0.0.1:1/lab/ats/standard"
    destination = f"{application_url}/submit"
    return {
        "application_id": application_id,
        "employer": employer,
        "role": role,
        "provider": "greenhouse",
        "requisition": "/lab/ats/standard",
        "application_url": application_url,
        "page_id": "fixture-page-v1",
        "target_fingerprint": "fixture-target-v1",
        "control_fingerprint": "fixture-control-v1",
        "control_selector": "form[data-testid='apply']",
        "root_selector": "main",
        "frame_url": application_url,
        "form_identity": "fixture-form-v1",
        "form_action": destination,
        "method": "POST",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "final_url": application_url,
        "expected_receipt_url": destination,
        "expected_final_url": destination,
        "destination": destination,
        "documents": [],
        "answers": [],
    }


@pytest.fixture()
def browser_server(tmp_path: Path):
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    app = create_app(settings)
    navigator = _LoopbackNavigator({})
    app.state.navigator = navigator
    app.state.handoff_manager = navigator
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    _wait_ready(base_url)
    try:
        yield _BrowserServer(app, base_url, navigator, server, thread)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture()
def page(browser_server: _BrowserServer):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        def start_review(route):  # noqa: ANN001
            request = route.request
            match = re.search(r"/api/applications/([^/?]+)/run", request.url)
            assert match is not None
            application_id = unquote(match.group(1))
            try:
                snapshot = browser_server.navigator.start(
                    application_id, RunMode.PREFILL
                )
            except DuplicateSessionError as exc:
                route.fulfill(
                    status=409,
                    headers={"content-type": "application/json"},
                    body=json.dumps({"detail": str(exc)}),
                )
                return
            route.fulfill(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "session_id": snapshot.session_id,
                        "handoff_session_id": snapshot.session_id,
                        "application_id": application_id,
                        "state": snapshot.state.value,
                    }
                ),
            )

        page.route(
            "**/api/applications/*/run?mode=prefill&headed=true",
            start_review,
        )
        # Production UI asks for an explicit confirmation before each
        # mutating Navigator action; accept it in the browser harness so
        # lifecycle assertions exercise the action rather than auto-dismiss.
        page.on("dialog", lambda dialog: dialog.accept())
        try:
            yield page
        finally:
            context.close()
            browser.close()


def test_verified_apply_starts_navigator_and_keeps_source_separate(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="Verified Loopback")
    browser_server.navigator.scripts[application_id] = {
        "state": "ACTIVE",
        "employer": "Verified Loopback",
        "role": "Verified Loopback Summer Analyst",
        "provider": "greenhouse",
        "destination": "http://127.0.0.1:1/lab/ats/standard",
    }

    page.goto(f"{browser_server.base_url}/opportunities")
    expect(page.get_by_role("button", name="Apply in Navigator")).to_have_count(1)
    expect(page.get_by_role("link", name="Open source")).to_have_count(1)
    expect(page.get_by_role("link", name="Open source")).to_have_attribute(
        "href", "http://127.0.0.1:1/source/verified-loopback"
    )
    assert page.locator("a[data-navigator-start]").count() == 0
    popups: list[Any] = []
    page.on("popup", lambda popup: popups.append(popup))
    page.get_by_role("button", name="Apply in Navigator").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    assert popups == []
    assert browser_server.navigator.starts == [application_id]


def test_unresolved_target_has_no_apply_action_and_explains_blocker(
    browser_server: _BrowserServer, page: Page
) -> None:
    _seed(
        browser_server.app,
        employer="Unresolved Loopback",
        target_status=TargetKind.LISTING.value,
        application_url=None,
        provider="",
        reason_codes=("listing_not_application",),
    )
    page.goto(f"{browser_server.base_url}/opportunities")
    expect(page.get_by_role("button", name="Apply in Navigator")).to_have_count(0)
    expect(page.get_by_text("Listing / not an application form")).to_have_count(1)
    expect(page.get_by_text("listing_not_application")).to_have_count(1)
    expect(page.get_by_role("link", name="Open source")).to_have_count(1)


@pytest.mark.parametrize(
    ("target_status", "label"),
    [
        (TargetKind.NON_HTML.value, "Non-HTML target"),
        (TargetKind.MISMATCH.value, "Employer or role mismatch"),
        (TargetKind.AUTH_WALL.value, "Authentication wall"),
        (TargetKind.BLOCKED.value, "Target blocked"),
    ],
)
def test_non_automatable_target_kinds_never_render_apply(
    browser_server: _BrowserServer, page: Page, target_status: str, label: str
) -> None:
    _seed(
        browser_server.app,
        employer=f"{target_status} Loopback",
        target_status=target_status,
        application_url=None,
        provider="",
        reason_codes=(target_status.casefold(),),
    )
    page.goto(f"{browser_server.base_url}/opportunities")
    expect(page.get_by_role("button", name="Apply in Navigator")).to_have_count(0)
    expect(page.get_by_text(label, exact=True)).to_have_count(1)


def test_popup_or_reload_resumes_stored_session_without_a_second_start(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="Resume Loopback")
    browser_server.navigator.scripts[application_id] = {"state": "ACTIVE"}
    page.goto(f"{browser_server.base_url}/opportunities")
    page.get_by_role("button", name="Apply in Navigator").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    session_id = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    page.reload()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    assert page.locator("[data-navigator-session]").get_attribute("data-navigator-session") == session_id
    assert browser_server.navigator.starts == [application_id]


def test_cancelled_session_is_cleared_and_apply_starts_a_fresh_session(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="Cancel Resume Loopback")
    browser_server.navigator.scripts[application_id] = {"state": "ACTIVE"}
    page.goto(f"{browser_server.base_url}/opportunities")
    button = page.get_by_role("button", name="Apply in Navigator")
    button.click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    first_session = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    page.get_by_role("button", name="Cancel").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("CANCELLED")
    expect(page.locator("[data-navigator-status]")).to_contain_text("cancelled")
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        application_id,
    ) is None

    button.click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    expect(page.locator("[data-navigator-session]")).to_have_attribute("data-navigator-session", "session-2")
    second_session = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    assert second_session != first_session
    assert browser_server.navigator.starts == [application_id, application_id]


@pytest.mark.parametrize("terminal_state", ["EXPIRED", "FAILED"])
def test_expired_or_failed_session_is_cleared_before_next_apply_start(
    browser_server: _BrowserServer, page: Page, terminal_state: str
) -> None:
    application_id, _ = _seed(browser_server.app, employer=f"{terminal_state} Loopback")
    browser_server.navigator.scripts[application_id] = {
        "state": terminal_state,
        "reason": f"loopback {terminal_state.casefold()}",
    }
    page.goto(f"{browser_server.base_url}/opportunities")
    button = page.get_by_role("button", name="Apply in Navigator")
    button.click()
    expect(page.locator("[data-navigator-state]")).to_contain_text(terminal_state)
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        application_id,
    ) is None
    first_session = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")

    button.click()
    expect(page.locator("[data-navigator-state]")).to_contain_text(terminal_state)
    expect(page.locator("[data-navigator-session]")).to_have_attribute("data-navigator-session", "session-2")
    second_session = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    assert second_session != first_session
    assert browser_server.navigator.starts == [application_id, application_id]


def test_missing_session_is_cleared_and_next_apply_starts_new_session(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="Missing Session Loopback")
    browser_server.navigator.scripts[application_id] = {"state": "ACTIVE"}
    page.goto(f"{browser_server.base_url}/opportunities")
    button = page.get_by_role("button", name="Apply in Navigator")
    button.click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    first_session = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    browser_server.navigator.sessions.pop(first_session)
    page.reload()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ERROR")
    expect(page.locator("[data-navigator-status]")).to_contain_text("status unavailable (404)")
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        application_id,
    ) is None

    button = page.get_by_role("button", name="Apply in Navigator")
    button.click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    expect(page.locator("[data-navigator-session]")).to_have_attribute("data-navigator-session", "session-2")
    second_session = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    assert second_session != first_session
    assert browser_server.navigator.starts == [application_id, application_id]


def test_cross_application_session_storage_mismatch_is_refused(
    browser_server: _BrowserServer, page: Page
) -> None:
    first_id, _ = _seed(browser_server.app, employer="First Identity Loopback")
    second_id, _ = _seed(browser_server.app, employer="Second Identity Loopback")
    browser_server.navigator.scripts[first_id] = {"state": "ACTIVE"}
    browser_server.navigator.scripts[second_id] = {"state": "ACTIVE"}
    page.goto(f"{browser_server.base_url}/opportunities")
    page.locator(f"[data-application-id='{second_id}'][data-navigator-start]").click()
    expect(page.locator(f"[data-application-id='{second_id}'] [data-navigator-state]")).to_contain_text("ACTIVE")
    second_session = page.locator(
        f"[data-application-id='{second_id}'] [data-navigator-session]"
    ).get_attribute("data-navigator-session")
    page.evaluate(
        "([applicationId, sessionId]) => sessionStorage.setItem(`argus:navigator:${applicationId}`, sessionId)",
        [first_id, second_session],
    )
    page.reload()
    first_panel = page.locator(f"[data-application-id='{first_id}']")
    expect(first_panel.locator("[data-navigator-state]")).to_contain_text("ERROR")
    expect(first_panel.locator("[data-navigator-status]")).to_contain_text("identity mismatch")
    assert first_panel.get_by_role("button", name="Continue").get_attribute("hidden") is not None
    expect(first_panel.get_by_role("button", name="Confirm this exact manifest")).to_have_count(0)
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        first_id,
    ) is None


def test_active_session_can_request_exact_manifest_without_submit(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="Manifest Request Loopback")
    browser_server.navigator.scripts[application_id] = {
        "state": "ACTIVE",
        "manifest": _exact_manifest(
            application_id,
            "Manifest Request Loopback",
            "Manifest Request Loopback Summer Analyst",
        ),
    }
    page.goto(f"{browser_server.base_url}/opportunities")
    page.get_by_role("button", name="Apply in Navigator").click()
    expect(page.get_by_role("button", name="Review exact manifest")).to_be_visible()
    page.get_by_role("button", name="Review exact manifest").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("FINAL REVIEW")
    expect(page.locator("[data-navigator-status]")).to_contain_text("one-time confirmation")
    assert browser_server.navigator.sessions["session-1"].confirm_count == 0


def test_page_uses_external_scripts_and_production_csp(
    browser_server: _BrowserServer, page: Page
) -> None:
    _seed(browser_server.app, employer="CSP Loopback")
    response = page.goto(f"{browser_server.base_url}/needs-you")
    assert response is not None
    csp = response.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert page.locator("script:not([src])").count() == 0
    assert page.locator("[onclick], [onmousedown], [onmouseup]").count() == 0
    assert page.locator("[style]").count() == 0


def test_human_required_continuation_keeps_session_and_never_claims_submit(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="CAPTCHA Loopback")
    browser_server.navigator.scripts[application_id] = {
        "state": "HUMAN_REQUIRED",
        "reason": "captcha_detected",
        "after_continue": "ACTIVE",
        "employer": "CAPTCHA Loopback",
        "role": "CAPTCHA Loopback Summer Analyst",
        "provider": "greenhouse",
        "destination": "http://127.0.0.1:1/lab/ats/standard",
    }
    page.goto(f"{browser_server.base_url}/opportunities")
    page.get_by_role("button", name="Apply in Navigator").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("HUMAN REQUIRED")
    expect(page.get_by_role("button", name="Continue")).to_be_visible()
    expect(page.get_by_role("button", name="Cancel")).to_be_visible()
    session_id = page.locator("[data-navigator-session]").get_attribute("data-navigator-session")
    page.get_by_role("button", name="Continue").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    assert page.locator("[data-navigator-session]").get_attribute("data-navigator-session") == session_id
    assert "submitted" not in page.locator("[data-navigator-status]").inner_text().casefold()
    assert browser_server.navigator.sessions[session_id].continue_count == 1


def test_source_resolution_202_binds_panel_and_finalizes_after_continue(
    browser_server: _BrowserServer, page: Page
) -> None:
    """A target-resolution handoff remains usable from the production UI.

    The first resolver call returns a typed session handoff.  Continue must
    reuse that exact session and the UI must poll the application-id-only
    resolver until the verified destination is persisted; no URL is supplied
    by the browser.
    """

    application_id, opportunity_id = _seed(
        browser_server.app,
        employer="Source Resolution Loopback",
        application_url=None,
        target_status=TargetKind.UNRESOLVED.value,
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "HUMAN_REQUIRED",
        "reason": "Visible source inspection is required",
        "after_continue": "ACTIVE",
    }
    resolver_calls = 0
    source_url = "http://127.0.0.1:1/source/source-resolution-loopback"
    destination = "http://127.0.0.1:1/lab/ats/standard"

    def resolver(context):  # noqa: ANN001
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls == 1:
            snapshot = browser_server.navigator.start(
                application_id, RunMode.REVIEW
            )
            return None, {
                "session_id": snapshot.session_id,
                "application_id": application_id,
                "state": "HUMAN_REQUIRED",
                "status": "awaiting_human",
                "headed": True,
                "can_continue": True,
                "can_cancel": True,
                "no_auto_submit": True,
                "resumable": True,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }
        session_id = next(iter(browser_server.navigator.sessions))
        assert browser_server.navigator.sessions[session_id].continue_count == 1
        return TargetResolution(
            source_url=source_url,
            final_url=destination,
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            reason_codes=("visible_browser_verified",),
            evidence={
                "synthetic_lab": True,
                "source_inspection": True,
                "provider": "greenhouse",
                "employer": context.employer,
                "role": context.role_title,
                "requisition": "/lab/ats/standard",
                "form_identity": "standard",
                "application_origin": origin_for_url(destination),
            },
        )

    browser_server.app.state.target_resolution_resolver = resolver
    page.goto(f"{browser_server.base_url}/applications")
    action = page.locator(
        f"[data-action='/api/applications/{application_id}/resolve-target']"
    )
    expect(action).to_be_visible()
    action.click()

    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text(
        "HUMAN REQUIRED", timeout=5_000
    )
    expect(panel.get_by_role("button", name="Continue")).to_be_visible()
    session_id = panel.locator("[data-navigator-session]").get_attribute(
        "data-navigator-session"
    )
    assert session_id

    panel.get_by_role("button", name="Continue").click()
    expect(page.locator("[data-toasts]")).to_contain_text(
        "Exact application destination verified", timeout=10_000
    )
    assert browser_server.navigator.sessions[session_id].continue_count == 1
    stored_url = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with browser_server.app.state.db.session_scope() as session:
            stored = session.get(Opportunity, opportunity_id)
            stored_url = stored.application_url if stored is not None else None
        if stored_url == destination:
            break
        time.sleep(0.05)
    assert stored_url == destination


def test_stale_status_response_cannot_overwrite_newer_terminal_state(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(browser_server.app, employer="Out of Order Loopback")
    browser_server.navigator.scripts[application_id] = {"state": "ACTIVE"}
    held: list[Any] = []
    status_calls = 0

    def hold_second_status(route):  # noqa: ANN001
        nonlocal status_calls
        status_calls += 1
        if status_calls == 2:
            held.append(route)
            return
        route.continue_()

    page.route("**/api/handoff/sessions/session-1", hold_second_status)
    page.goto(f"{browser_server.base_url}/opportunities")
    page.get_by_role("button", name="Apply in Navigator").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ACTIVE")
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held, "the second status poll was not captured"

    page.get_by_role("button", name="Cancel").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("CANCELLED")
    held[0].fulfill(
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "session_id": "session-1",
                "application_id": application_id,
                "state": "ACTIVE",
                "status": "ACTIVE",
                "summary": {},
                "manifest": {},
            }
        ),
    )
    page.wait_for_timeout(100)
    expect(page.locator("[data-navigator-state]")).to_contain_text("CANCELLED")


def test_source_resolution_reload_preserves_mode_and_exact_session(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, opportunity_id = _seed(
        browser_server.app,
        employer="Source Reload Loopback",
        application_url=None,
        target_status=TargetKind.UNRESOLVED.value,
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "HUMAN_REQUIRED",
        "reason": "Visible source inspection is required",
    }
    resolver_calls = 0
    distinct_session_ids: set[str] = set()
    destination = "http://127.0.0.1:1/lab/ats/source-reload"

    def resolver(context):  # noqa: ANN001
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls == 1:
            snapshot = browser_server.navigator.start(application_id, RunMode.REVIEW)
            distinct_session_ids.add(snapshot.session_id)
            return None, {
                "session_id": snapshot.session_id,
                "application_id": application_id,
                "state": "HUMAN_REQUIRED",
                "status": "awaiting_human",
                "can_continue": True,
                "can_cancel": True,
                "no_auto_submit": True,
                "resumable": True,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }
        session_id = next(iter(browser_server.navigator.sessions))
        distinct_session_ids.add(session_id)
        if browser_server.navigator.sessions[session_id].continue_count == 0:
            return None, {
                "session_id": session_id,
                "application_id": application_id,
                "state": "HUMAN_REQUIRED",
                "status": "awaiting_human",
                "can_continue": True,
                "can_cancel": True,
                "no_auto_submit": True,
                "resumable": True,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }
        assert browser_server.navigator.sessions[session_id].continue_count == 1
        return TargetResolution(
            source_url="http://127.0.0.1:1/source/source-reload-loopback",
            final_url=destination,
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            reason_codes=("visible_browser_verified",),
            evidence={
                "synthetic_lab": True,
                "source_inspection": True,
                "provider": "greenhouse",
                "employer": context.employer,
                "role": context.role_title,
                "requisition": "/lab/ats/source-reload",
                "form_identity": "source-reload",
                "application_origin": origin_for_url(destination),
            },
        )

    browser_server.app.state.target_resolution_resolver = resolver
    page.goto(f"{browser_server.base_url}/applications")
    action = page.locator(
        f"[data-action='/api/applications/{application_id}/resolve-target']"
    )
    action.click()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text("HUMAN REQUIRED")
    session_id = panel.locator("[data-navigator-session]").get_attribute(
        "data-navigator-session"
    )
    stored = page.evaluate(
        "applicationId => JSON.parse(sessionStorage.getItem(`argus:navigator:${applicationId}`))",
        application_id,
    )
    assert stored is not None
    assert stored["application_id"] == application_id
    assert stored["session_id"] == session_id
    assert stored["mode"] == "source_resolution"
    assert stored["source_resolution"] is True

    page.reload()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text("HUMAN REQUIRED")
    expect(panel.locator("[data-navigator-session]")).to_have_attribute(
        "data-navigator-session", session_id
    )
    deadline = time.monotonic() + 5
    while resolver_calls < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert resolver_calls >= 2, "reload did not resume source-resolution polling before Continue"
    starts = len(browser_server.navigator.starts)
    assert starts == 1
    assert distinct_session_ids == {session_id}
    panel.get_by_role("button", name="Continue").click()
    expect(page.locator("[data-toasts]")).to_contain_text(
        "Exact application destination verified", timeout=10_000
    )
    with browser_server.app.state.db.session_scope() as session:
        stored_opportunity = session.get(Opportunity, opportunity_id)
        assert stored_opportunity is not None
        assert stored_opportunity.application_url == destination


def test_source_resolution_terminal_response_stops_all_poll_timers(
    browser_server: _BrowserServer, page: Page
) -> None:
    page.add_init_script(
        """
        (() => {
          const nativeSetTimeout = window.setTimeout.bind(window);
          const nativeClearTimeout = window.clearTimeout.bind(window);
          const active = new Map();
          window.__argusTimerStats = {active};
          window.setTimeout = (callback, delay, ...args) => {
            const id = nativeSetTimeout(() => {
              active.delete(id);
              callback(...args);
            }, delay);
            active.set(id, Number(delay) || 0);
            return id;
          };
          window.clearTimeout = id => {
            active.delete(id);
            return nativeClearTimeout(id);
          };
        })();
        """
    )
    application_id, _ = _seed(
        browser_server.app,
        employer="Source Terminal Loopback",
        application_url=None,
        target_status=TargetKind.UNRESOLVED.value,
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "HUMAN_REQUIRED",
        "reason": "Visible source inspection is required",
    }
    resolver_calls = 0

    def resolver(_context):  # noqa: ANN001
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls == 1:
            session = browser_server.navigator.start(application_id, RunMode.REVIEW)
            return None, {
                "session_id": session.session_id,
                "application_id": application_id,
                "state": "HUMAN_REQUIRED",
                "status": "awaiting_human",
                "can_continue": True,
                "can_cancel": True,
                "no_auto_submit": True,
                "resumable": True,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }
        session = next(iter(browser_server.navigator.sessions.values()))
        return None, {
            "session_id": session.session_id,
            "application_id": application_id,
            "state": "CANCELLED",
            "status": "CANCELLED",
            "can_continue": False,
            "can_cancel": False,
            "no_auto_submit": True,
            "resumable": False,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    browser_server.app.state.target_resolution_resolver = resolver
    page.goto(f"{browser_server.base_url}/applications")
    action = page.locator(
        f"[data-action='/api/applications/{application_id}/resolve-target']"
    )
    action.click()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    panel.get_by_role("button", name="Continue").click()
    expect(panel.locator("[data-navigator-state]")).to_contain_text("CANCELLED")
    page.wait_for_timeout(1_200)
    assert resolver_calls == 2
    assert page.evaluate(
        "() => Array.from(window.__argusTimerStats.active.values()).filter(delay => delay === 500 || delay === 1200).length"
    ) == 0


def test_source_resolution_rejects_cross_bound_handoff_identity_and_reenables_button(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Source Cross Bind Loopback",
        application_url=None,
        target_status=TargetKind.UNRESOLVED.value,
    )

    def resolver(_context):  # noqa: ANN001
        snapshot = browser_server.navigator.start(application_id, RunMode.REVIEW)
        return None, {
            "session_id": snapshot.session_id,
            "application_id": "different-application",
            "state": "HUMAN_REQUIRED",
            "status": "awaiting_human",
            "can_continue": True,
            "can_cancel": True,
            "no_auto_submit": True,
            "resumable": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    browser_server.app.state.target_resolution_resolver = resolver
    page.goto(f"{browser_server.base_url}/applications")
    action = page.locator(
        f"[data-action='/api/applications/{application_id}/resolve-target']"
    )
    action.click()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text("ERROR")
    assert panel.get_by_role("button", name="Continue").get_attribute("hidden") is not None
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        application_id,
    ) is None
    expect(action).to_be_enabled()


def test_source_resolution_binding_and_cancel_reenable_button_for_fresh_retry(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Source Retry Loopback",
        application_url=None,
        target_status=TargetKind.UNRESOLVED.value,
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "HUMAN_REQUIRED",
        "reason": "Visible source inspection is required",
    }
    resolver_calls = 0

    def resolver(_context):  # noqa: ANN001
        nonlocal resolver_calls
        resolver_calls += 1
        snapshot = browser_server.navigator.start(application_id, RunMode.REVIEW)
        return None, {
            "session_id": snapshot.session_id,
            "application_id": application_id,
            "state": "HUMAN_REQUIRED",
            "status": "awaiting_human",
            "can_continue": True,
            "can_cancel": True,
            "no_auto_submit": True,
            "resumable": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    browser_server.app.state.target_resolution_resolver = resolver
    page.goto(f"{browser_server.base_url}/applications")
    action = page.locator(
        f"[data-action='/api/applications/{application_id}/resolve-target']"
    )
    action.click()
    expect(action).to_be_enabled()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    panel.get_by_role("button", name="Cancel").click()
    expect(panel.locator("[data-navigator-state]")).to_contain_text("CANCELLED")
    expect(action).to_be_enabled()
    action.click()
    expect(panel.locator("[data-navigator-state]")).to_contain_text("HUMAN REQUIRED")
    assert resolver_calls == 2


def test_source_resolution_affordances_require_capabilities_resumability_and_expiry(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Source Capability Loopback",
        application_url=None,
        target_status=TargetKind.UNRESOLVED.value,
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "HUMAN_REQUIRED",
        "reason": "Visible source inspection is required",
    }

    def resolver(_context):  # noqa: ANN001
        snapshot = browser_server.navigator.start(application_id, RunMode.REVIEW)
        return None, {
            "session_id": snapshot.session_id,
            "application_id": application_id,
            "state": "HUMAN_REQUIRED",
            "status": "awaiting_human",
            "can_continue": False,
            "can_cancel": True,
            "no_auto_submit": True,
            "resumable": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    browser_server.app.state.target_resolution_resolver = resolver
    page.goto(f"{browser_server.base_url}/applications")
    page.locator(
        f"[data-action='/api/applications/{application_id}/resolve-target']"
    ).click()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text("HUMAN REQUIRED")
    assert panel.get_by_role("button", name="Continue").get_attribute("hidden") is not None
    expect(panel.get_by_role("button", name="Cancel")).to_be_visible()


def test_final_review_shows_exact_manifest_and_one_time_confirmation(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Manifest Loopback",
        state="READY_TO_SUBMIT",
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "FINAL_REVIEW",
        "reason": "ready_to_submit",
        "manifest": {
            **_exact_manifest(
                application_id,
                "Manifest Loopback",
                "Manifest Loopback Summer Analyst",
            ),
            "documents": [{"id": "cv-fixture", "kind": "CV · approved", "sha256": "c" * 64, "approved": True}],
            "answers": [{"id": "answer-fixture", "canonical_key": "work_authorisation · verified", "sha256": "a" * 64, "approved": True, "sensitive": False}],
        },
    }
    page.goto(f"{browser_server.base_url}/applications/{application_id}")
    page.get_by_role("button", name="Review exact manifest").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("FINAL REVIEW")
    expect(page.get_by_text("Manifest Loopback", exact=True)).to_have_count(1)
    expect(page.get_by_text(application_id, exact=True)).to_have_count(1)
    expect(page.get_by_text(re.compile("CV · approved"))).to_have_count(1)
    confirm_requests: list[dict[str, object]] = []
    page.on(
        "request",
        lambda request: confirm_requests.append(json.loads(request.post_data or "{}"))
        if request.method == "POST" and request.url.endswith("/confirm")
        else None,
    )
    page.get_by_role("button", name="Confirm this exact manifest").click()
    page.wait_for_timeout(300)
    state_text = page.locator("[data-navigator-state]").inner_text()
    assert "CONFIRMED" in state_text, page.locator("[data-navigator-status]").inner_text()
    expect(page.locator("[data-navigator-status]")).to_contain_text("no submit click")
    assert browser_server.navigator.sessions["session-1"].confirm_count == 1
    assert confirm_requests == [{"application_id": application_id}]


@pytest.mark.parametrize(
    ("result_state", "expected_status"),
    [
        ("CONFIRMATION_VERIFIED", "Submission confirmed by the exact receipt"),
        ("SUBMISSION_UNKNOWN", "Do not retry"),
    ],
)
def test_submit_authority_is_exactly_one_request_and_terminal_outcome_is_truthful(
    browser_server: _BrowserServer,
    page: Page,
    result_state: str,
    expected_status: str,
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer=f"One Shot {result_state}",
        state="READY_TO_SUBMIT",
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "FINAL_REVIEW",
        "reason": "ready_to_submit",
        "manifest": {
            **_exact_manifest(
                application_id,
                f"One Shot {result_state}",
                f"One Shot {result_state} Summer Analyst",
            ),
            "documents": [{"id": "cv-fixture", "kind": "CV · approved", "sha256": "c" * 64, "approved": True}],
            "answers": [{"id": "answer-fixture", "canonical_key": "work_authorisation · verified", "sha256": "a" * 64, "approved": True, "sensitive": False}],
        },
    }
    submit_requests: list[dict[str, object]] = []

    def fulfill_submit(route) -> None:  # noqa: ANN001
        request = route.request
        if request.method == "POST" and "/run" in request.url:
            submit_requests.append(json.loads(request.post_data or "{}"))
            route.fulfill(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps(
                    {
                        "state": result_state,
                        "application_id": application_id,
                        "session_id": "session-1",
                        "reason": "loopback terminal outcome",
                    }
                ),
            )
            return
        route.continue_()

    page.route(
        "**/api/applications/*/run?mode=submit&headed=true",
        fulfill_submit,
    )
    page.goto(f"{browser_server.base_url}/applications/{application_id}")
    page.get_by_role("button", name="Review exact manifest").click()
    page.get_by_role("button", name="Confirm this exact manifest").click()
    submit_button = page.get_by_role("button", name="Submit this exact application once")
    expect(submit_button).to_be_visible()

    # Dispatch two synchronous clicks before the first fetch resolves.  The
    # browser-side one-shot guard must leave exactly one network request.
    submit_button.evaluate("button => { button.click(); button.click(); }")
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text(
        result_state.replace("_", " ")
    )
    expect(panel.locator("[data-navigator-status]")).to_contain_text(expected_status)
    expect(panel.locator("[data-navigator-submit]")).to_have_count(0)
    cleared = panel.evaluate(
        "panel => ({session: panel.dataset.sessionId || null, confirmed: panel.dataset.navigatorConfirmed || null, manifest: panel.querySelector('[data-navigator-manifest]')?.textContent || ''})"
    )
    assert cleared == {"session": None, "confirmed": None, "manifest": ""}
    assert len(submit_requests) == 1
    assert set(submit_requests[0]) == {"application_id", "session_id", "authority_id"}
    assert submit_requests[0]["application_id"] == application_id
    assert submit_requests[0]["session_id"] == "session-1"
    assert str(submit_requests[0]["authority_id"])


def test_confirmed_submit_authority_is_memory_only_and_reload_cannot_resurrect_it(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Reload Authority Loopback",
        state="READY_TO_SUBMIT",
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "FINAL_REVIEW",
        "reason": "ready_to_submit",
        "manifest": _exact_manifest(
            application_id,
            "Reload Authority Loopback",
            "Reload Authority Loopback Summer Analyst",
        ),
    }
    page.goto(f"{browser_server.base_url}/applications/{application_id}")
    page.get_by_role("button", name="Review exact manifest").click()
    page.get_by_role("button", name="Confirm this exact manifest").click()
    submit_button = page.get_by_role("button", name="Submit this exact application once")
    expect(submit_button).to_be_visible()

    page.reload()
    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text("CONFIRMED")
    expect(panel.locator("[data-navigator-submit]")).to_have_count(0)
    assert panel.evaluate(
        "panel => ({session: panel.dataset.sessionId || null, confirmed: panel.dataset.navigatorConfirmed || null})"
    ) == {"session": None, "confirmed": None}
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        application_id,
    ) is None


def test_submit_authority_http_refusal_clears_session_and_cannot_retry(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Refused Authority Loopback",
        state="READY_TO_SUBMIT",
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "FINAL_REVIEW",
        "reason": "ready_to_submit",
        "manifest": _exact_manifest(
            application_id,
            "Refused Authority Loopback",
            "Refused Authority Loopback Summer Analyst",
        ),
    }
    requests: list[dict[str, object]] = []

    def refuse_submit(route) -> None:  # noqa: ANN001
        requests.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=409,
            headers={"content-type": "application/json"},
            body=json.dumps({"detail": "authority expired or changed"}),
        )

    page.route(
        "**/api/applications/*/run?mode=submit&headed=true",
        refuse_submit,
    )
    page.goto(f"{browser_server.base_url}/applications/{application_id}")
    page.get_by_role("button", name="Review exact manifest").click()
    page.get_by_role("button", name="Confirm this exact manifest").click()
    page.get_by_role("button", name="Submit this exact application once").click()

    panel = page.locator(
        f"[data-navigator-panel][data-application-id='{application_id}']"
    )
    expect(panel.locator("[data-navigator-state]")).to_contain_text("BLOCKED")
    expect(panel.locator("[data-navigator-status]")).to_contain_text("Do not retry")
    expect(panel.locator("[data-navigator-submit]")).to_have_count(0)
    assert panel.evaluate(
        "panel => ({session: panel.dataset.sessionId || null, confirmed: panel.dataset.navigatorConfirmed || null})"
    ) == {"session": None, "confirmed": None}
    assert page.evaluate(
        "applicationId => sessionStorage.getItem(`argus:navigator:${applicationId}`)",
        application_id,
    ) is None
    assert len(requests) == 1
    assert set(requests[0]) == {"application_id", "session_id", "authority_id"}


def test_manifest_identity_mismatch_exposes_no_confirmation_action(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Manifest Identity Loopback",
        state="READY_TO_SUBMIT",
    )
    browser_server.navigator.scripts[application_id] = {
        "state": "FINAL_REVIEW",
        "manifest": {
            "application_id": "different-application",
            "employer": "Manifest Identity Loopback",
            "role": "Manifest Identity Loopback Summer Analyst",
        },
    }
    page.goto(f"{browser_server.base_url}/applications/{application_id}")
    page.get_by_role("button", name="Review exact manifest").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("ERROR")
    expect(page.get_by_role("button", name="Confirm this exact manifest")).to_have_count(0)
    assert browser_server.navigator.sessions["session-1"].confirm_count == 0


def test_unknown_and_needs_oa_are_human_only_and_not_success(
    browser_server: _BrowserServer, page: Page
) -> None:
    unknown_id, _ = _seed(
        browser_server.app,
        employer="Unknown Loopback",
        state="SUBMISSION_UNKNOWN",
    )
    oa_id, _ = _seed(browser_server.app, employer="OA Loopback", state="NEEDS_OA")
    page.goto(f"{browser_server.base_url}/needs-you")
    expect(page.get_by_text("SUBMISSION UNKNOWN", exact=True)).to_have_count(1)
    expect(page.get_by_text("Do not retry", exact=False)).to_have_count(1)
    expect(page.get_by_text("NEEDS OA", exact=True)).to_have_count(1)
    unknown_card = page.locator(f"[data-application-id='{unknown_id}']")
    oa_card = page.locator(f"[data-application-id='{oa_id}']")
    expect(unknown_card.get_by_role("button", name="Apply in Navigator")).to_have_count(0)
    expect(oa_card.get_by_role("button", name="Apply in Navigator")).to_have_count(0)


def test_terminal_application_state_has_no_navigator_start_action(
    browser_server: _BrowserServer, page: Page
) -> None:
    application_id, _ = _seed(
        browser_server.app,
        employer="Submitted Loopback",
        state="SUBMITTED",
    )
    page.goto(f"{browser_server.base_url}/opportunities")
    card = page.locator(f"[data-application-id='{application_id}']")
    expect(card.get_by_role("button", name="Apply in Navigator")).to_have_count(0)
    expect(card.get_by_text("Navigator unavailable", exact=True)).to_have_count(1)


def test_blocked_start_and_network_error_stay_visible_as_errors(
    browser_server: _BrowserServer, page: Page
) -> None:
    blocked_id, _ = _seed(browser_server.app, employer="Blocked Loopback")
    browser_server.navigator.scripts[blocked_id] = {"start_error": "target blocked"}
    page.goto(f"{browser_server.base_url}/opportunities")
    page.get_by_role("button", name="Apply in Navigator").click()
    expect(page.locator("[data-navigator-state]")).to_contain_text("BLOCKED")
    expect(page.locator("[data-navigator-status]")).to_contain_text("target blocked")
    assert "submitted" not in page.locator("[data-navigator-status]").inner_text().casefold()

    error_id, _ = _seed(browser_server.app, employer="Network Error Loopback")
    browser_server.navigator.scripts[error_id] = {"state": "ACTIVE"}
    page.goto(f"{browser_server.base_url}/opportunities")
    button = page.locator(f"[data-application-id='{error_id}'][data-navigator-start]")
    page.route(
        "**/api/applications/*/run?mode=prefill&headed=true",
        lambda route: route.abort(),
    )
    button.click()
    expect(page.locator(f"[data-application-id='{error_id}'] [data-navigator-state]")).to_contain_text(
        "ERROR"
    )
