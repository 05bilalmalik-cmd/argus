from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.adapters.lever import LeverAdapter
from app.automation.adapters.registry import AdapterRegistry
from app.automation.targets import TargetResolution
from app.domain.targets import TargetKind


_LOCAL_TEST_URL = ""


@pytest.fixture(scope="module", autouse=True)
def _local_http_server():
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"<html><body>local adapter test</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    global _LOCAL_TEST_URL
    _LOCAL_TEST_URL = f"http://127.0.0.1:{server.server_port}/lab/ats/standard"
    yield
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _bound_adapter(page=None) -> GenericAdapter:
    target_url = str(getattr(page, "url", "") or "") or _LOCAL_TEST_URL
    return AdapterRegistry().detect(
        TargetResolution(
            source_url=target_url,
            final_url=target_url,
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            evidence={
                "synthetic_lab": True,
                "ats": "greenhouse",
                "role": "Summer Analyst",
                "requisition": "REQ-",
            },
        )
    )


def test_generic_adapter_ignores_css_hidden_controls_and_uses_only_enabled_submit() -> None:
    html = """
    <form data-role="Summer Analyst" action="/apply/REQ-1" onsubmit="event.preventDefault(); window.submits = (window.submits || 0) + 1">
      <label for="visible">First name</label><input id="visible" name="first_name">
      <label for="hidden">Hidden duplicate</label>
      <input id="hidden" name="hidden" style="display:none">
      <button id="disabled" type="submit" disabled>Submit application</button>
      <button id="hidden-submit" type="submit" style="display:none">Submit application</button>
      <button id="visible-submit" type="submit">Submit application</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)

        adapter = _bound_adapter(page)
        fields = adapter.inspect(page)
        adapter.submit(page)
        submits = page.evaluate("window.submits || 0")

        browser.close()

    assert [field.question.name for field in fields] == ["first_name"]
    assert submits == 1


def test_generic_adapter_fails_closed_when_final_submit_is_ambiguous() -> None:
    html = """
          <form data-role="Summer Analyst" action="/apply/REQ-1">
      <input name="first_name">
      <button type="submit">Submit application</button>
      <button type="submit">Submit application</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)

        with pytest.raises(RuntimeError, match="(?i)ambiguous"):
            _bound_adapter(page).submit(page)

        browser.close()


def test_generic_adapter_fails_closed_when_final_submit_is_absent() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<form><button type="button">Save draft</button></form>')

        with pytest.raises(RuntimeError, match="No visible submission control"):
            GenericAdapter().submit(page)

        browser.close()


def _verified_provider_resolution(page, provider: str) -> TargetResolution:
    return TargetResolution(
        source_url=page.url,
        final_url=page.url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider=provider,
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": provider,
            "role": "Summer Analyst",
            "requisition": "REQ-1",
        },
    )


@pytest.mark.parametrize("adapter_type", [GreenhouseAdapter, LeverAdapter])
def test_embedded_provider_apply_ignores_job_alert_control(adapter_type) -> None:
    html = """
    <main data-ats="greenhouse" data-employer="Acme" data-role="Summer Analyst">
      <button id="alerts" type="button">Apply for job alerts</button>
    </main>
    """.replace('data-ats="greenhouse"', 'data-ats="' + adapter_type.name + '"')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)
        page.evaluate(
            "document.querySelector('#alerts').addEventListener('click', () => window.alertClicks = (window.alertClicks || 0) + 1)"
        )

        adapter = adapter_type(_verified_provider_resolution(page, adapter_type.name))
        assert adapter.enter_application_flow(page) is False
        assert page.evaluate("window.alertClicks || 0") == 0

        browser.close()


@pytest.mark.parametrize("adapter_type", [GreenhouseAdapter, LeverAdapter])
def test_embedded_provider_advance_uses_control_shape_not_validation_text(adapter_type) -> None:
    html = """
    <section id="step" data-step="one">
      <form action="/application/one">
        <label>First name <input id="first" name="first_name" required></label>
        <button id="next" type="button">Next</button>
      </form>
      <script>
        document.querySelector('#next').addEventListener('click', () => {
          const first = document.querySelector('#first');
          if (!first.value) {
            const error = document.createElement('div');
            error.className = 'validation-error';
            error.textContent = 'Please provide a first name';
            document.querySelector('#step').append(error);
            return;
          }
          document.querySelector('#step').outerHTML = `
            <section id="step" data-step="two">
              <form action="/application/two">
                <label>Last name <input id="last" name="last_name" required></label>
                <button id="next" type="button">Next</button>
              </form>
            </section>`;
          document.querySelector('#next').addEventListener('click', () => {});
        });
      </script>
    </section>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)
        adapter = adapter_type()

        assert adapter.advance_one_step(page, max_wait_steps=2) is False
        assert page.locator('.validation-error').count() == 1

        page.locator('#first').fill('Alex')
        assert adapter.advance_one_step(page, max_wait_steps=2) is True
        assert page.locator('#last').count() == 1

        browser.close()


@pytest.mark.parametrize("adapter_type", [GreenhouseAdapter, LeverAdapter])
def test_embedded_provider_detects_mfa_human_boundary(adapter_type) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(
            "<main>Enter the one-time passcode from your authenticator app.</main>"
        )

        found, reason = adapter_type().detect_human_boundary(page)
        assert found is True
        assert reason == "mfa_required"

        browser.close()
