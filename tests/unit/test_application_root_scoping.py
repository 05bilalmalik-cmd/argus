# TDD: R10 — application-root scoping + sibling-relative nth-of-type.
# Cookie/search/newsletter controls outside an application root must never
# be inspected as application questions; nth-of-type must count siblings,
# not document-wide positions.
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.registry import AdapterRegistry
from app.automation.classifier import DeterministicClassifier
from app.automation.runner import build_fill_plan
from app.automation.targets import FormHandle, TargetResolution
from app.domain.targets import TargetKind


class _FakeLocator:
    """Minimal page.evaluate stand-in returning canned JS data."""

    def __init__(self, raw):
        self._raw = raw

    def evaluate(self, *_args, **_kwargs):
        return self._raw


class _FakePage:
    def __init__(self, raw, url="http://127.0.0.1:8787/lab/ats/standard"):
        self._raw = raw
        self.url = url

    def evaluate(self, script, *args):
        # The inspect script must scope its queries; we assert on the script
        # content rather than executing it here (Playwright executes it in
        # the e2e lab fixtures).
        return self._raw


def test_inspect_script_scopes_to_application_root():
    from app.automation.adapters.generic import _INSPECT_SCRIPT

    # The script must locate a scoped application root, not query the whole
    # document for controls.
    assert "applicationRoot" in _INSPECT_SCRIPT or "findApplicationRoot" in _INSPECT_SCRIPT
    # The old document-wide control scan must be gone.
    assert "document.querySelectorAll('input" not in _INSPECT_SCRIPT
    assert 'document.querySelectorAll("input' not in _INSPECT_SCRIPT


def test_selector_nth_of_type_is_sibling_relative():
    from app.automation.adapters.generic import _INSPECT_SCRIPT

    # The selector helper must use sibling counting (previousElementSibling
    # or :scope relative traversal), not a document-wide tag index.
    assert "previousElementSibling" in _INSPECT_SCRIPT or "parent.children" in _INSPECT_SCRIPT
    # Old broken pattern: peers.indexOf(el) over document.querySelectorAll(tag)
    assert "document.querySelectorAll(tag)" not in _INSPECT_SCRIPT


def test_inspect_reports_root_absence():
    adapter = GenericAdapter()
    # When the scanner reports no application root was found, the adapter
    # must surface that to the runner via the readiness evidence channel.
    raw = {"root_found": False, "fields": []}
    page = _FakePage(raw)
    fields, readiness = adapter.inspect_with_evidence(page)
    assert fields == []
    assert readiness["root_found"] is False


def test_inspect_scoped_fields_and_root():
    adapter = _externally_bound_adapter()
    raw = {
        "root_found": True,
        "root_description": "form#application-form",
        "submit_present": True,
        "fields": [
            {
                "selector": "#email",
                "label": "Email address",
                "field_type": "email",
                "name": "email",
                "placeholder": "",
                "required": True,
                "options": [],
                "control_type": "email",
                "value_attribute": "",
            }
        ],
        "root_identity": {
            "employer": "Acme Capital",
            "role": "Summer Analyst",
            "requisition": "REQ-42",
            "form_identity": "application-form",
            "provider": "",
        },
    }
    fields, readiness = adapter.inspect_with_evidence(page := _FakePage(raw))
    assert len(fields) == 1
    assert readiness["root_found"] is True
    assert readiness["submit_present"] is True


@pytest.fixture(scope="module")
def browser_page():
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"<html><body><h1>local test page</h1></body></html>"
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
    runtime = sync_playwright().start()
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{server.server_port}/lab/ats/standard")
        yield page
    finally:
        page.close()
        browser.close()
        runtime.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_search_form_named_first_name_cannot_become_ready(browser_page) -> None:
    """Employer/role text plus a deceptively named search box is not a form."""

    browser_page.set_content(
        """
        <h1>Acme Capital — Summer Analyst</h1>
        <form id="site-search" role="search" action="/search">
          <label>Search roles <input name="firstName" type="search"></label>
          <button type="submit">Search</button>
        </form>
        """
    )
    fields, readiness = GenericAdapter().inspect_with_evidence(browser_page)
    plan = build_fill_plan(
        fields,
        DeterministicClassifier(),
        {},
        answer_lookup=lambda *_args: None,
        document_lookup=lambda *_args: None,
        adapter_name="generic",
        step_evidence=readiness,
    )

    assert fields == []
    assert readiness["root_found"] is False
    assert plan.risk.level == 4
    assert "application_form_not_found" in plan.risk.blocking_codes


def test_smallest_application_form_excludes_newsletter_sibling(browser_page) -> None:
    browser_page.set_content(
        """
        <section id="career-widget" data-ats-application>
          <form id="newsletter" action="/subscribe">
            <label>Newsletter email <input name="newsletterEmail" type="email" required></label>
            <button type="submit">Subscribe</button>
          </form>
          <form id="application-form" data-role="Summer Analyst" action="/apply/REQ-1">
            <label>First name <input name="firstName" required></label>
            <label>Last name <input name="lastName" required></label>
            <label>Email <input name="email" type="email" required></label>
            <button id="application-submit" type="submit">Submit application</button>
          </form>
        </section>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    fields, readiness = adapter.inspect_with_evidence(browser_page)

    assert readiness["root_selector"] == "#application-form"
    assert {field.question.name for field in fields} == {"firstName", "lastName", "email"}
    assert all("#application-form" in field.selector for field in fields)
    assert all("newsletter" not in field.question.name.casefold() for field in fields)


def test_submission_target_is_bound_to_the_inspected_root(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="newsletter" action="/subscribe">
          <input name="email"><button id="marketing-submit">Submit</button>
        </form>
        <form id="application-form" data-role="Summer Analyst" action="/apply/REQ-1" data-ats-application>
          <input name="firstName"><input name="lastName"><input name="email">
          <button id="application-submit" type="submit">Submit application</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    _fields, readiness = adapter.inspect_with_evidence(browser_page)
    handle = adapter._last_form_handle
    assert handle is not None

    target = adapter.submission_target(browser_page, handle)

    assert target is not None
    assert target.root_selector == "#application-form"
    assert target.control_selector == "#application-form > #application-submit"
    assert target.form_action.endswith("/apply/REQ-1")


def test_scoped_selectors_resolve_nested_controls_inside_application_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" action="/apply/REQ-2">
          <div><label>First name <span><input id="nested-first" name="firstName"></span></label></div>
          <fieldset>
            <legend>Sponsorship</legend>
            <label><input type="radio" name="sponsor" value="yes">Yes</label>
            <label><input type="radio" name="sponsor" value="no">No</label>
          </fieldset>
          <button type="submit">Submit application</button>
        </form>
        """
    )
    fields, _readiness = _mechanics_adapter(browser_page).inspect_with_evidence(browser_page)
    by_name = {field.question.name: field for field in fields}

    assert browser_page.locator(by_name["firstName"].selector).count() == 1
    assert browser_page.locator(by_name["sponsor"].selector).count() == 2


def test_known_greenhouse_form_without_final_submit_is_still_inspectable(
    browser_page,
) -> None:
    """A save-draft-only step is a form, but must not invent a submit target."""

    browser_page.set_content(
        """
        <form id="grnhse_app" class="greenhouse-form" data-role="Summer Analyst" action="/next/REQ-3">
          <label>First name <input name="firstName" required></label>
          <label>Last name <input name="lastName" required></label>
          <label>Email <input name="email" type="email" required></label>
          <button type="button">Save draft</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    fields, readiness = adapter.inspect_with_evidence(browser_page)

    assert readiness["root_selector"] == "#grnhse_app"
    assert readiness["submit_present"] is False
    assert {field.question.name for field in fields} == {
        "firstName",
        "lastName",
        "email",
    }
    handle = FormHandle(
        page_id=str(id(browser_page)),
        frame_url=browser_page.url,
        root_selector=readiness["root_selector"],
        provider="greenhouse",
        evidence={"root_found": True},
    )
    assert adapter.submission_target(browser_page, handle) is None


def test_textual_submit_label_on_non_submit_button_is_not_a_target(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="grnhse_app" class="greenhouse-form" data-role="Summer Analyst" action="/next/REQ-3b">
          <input name="firstName"><input name="lastName">
          <button type="button">Submit application</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    adapter.inspect_with_evidence(browser_page)
    handle = adapter._last_form_handle

    assert handle is not None
    assert adapter.submission_target(browser_page, handle) is None


def test_non_button_submit_automation_marker_is_not_a_target(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-ats-application action="/apply/REQ-3c">
          <input name="firstName"><input name="lastName">
          <div data-automation-id="submitButton">Submit application</div>
        </form>
        """
    )
    adapter = GenericAdapter()
    fields, readiness = adapter.inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False
    assert adapter._last_form_handle is None


def test_explicitly_marked_newsletter_form_is_not_an_application_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <h1>Acme Capital — Summer Analyst</h1>
        <form id="newsletter" data-ats-application action="/subscribe">
          <label>Newsletter email <input name="email" type="email" required></label>
          <button type="submit">Subscribe</button>
        </form>
        """
    )
    adapter = GenericAdapter()
    fields, readiness = adapter.inspect_with_evidence(browser_page)
    plan = build_fill_plan(
        fields,
        DeterministicClassifier(),
        {},
        answer_lookup=lambda *_args: None,
        document_lookup=lambda *_args: None,
        adapter_name="greenhouse",
        step_evidence=readiness,
    )

    assert fields == []
    assert readiness["root_found"] is False
    assert plan.risk.level == 4
    assert "application_form_not_found" in plan.risk.blocking_codes


def test_explicitly_marked_preferences_form_is_not_an_application_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="preferences" data-ats-application action="/preferences">
          <label>Email <input name="email" type="email" required></label>
          <button type="submit">Submit application</button>
        </form>
        """
    )
    fields, readiness = GenericAdapter().inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False


def test_deceptive_apply_form_with_only_email_is_not_an_application_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="apply-now" action="/post-application">
          <label>Email <input name="email" type="email" required></label>
          <button type="submit">Apply now</button>
        </form>
        """
    )
    fields, readiness = GenericAdapter().inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False


def test_known_ats_marker_with_only_email_is_not_an_application_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="grnhse_app" class="greenhouse-form" action="/post-application">
          <label>Email <input name="email" type="email" required></label>
          <button type="submit">Apply now</button>
        </form>
        """
    )
    fields, readiness = GenericAdapter().inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False


def test_unmarked_apply_form_with_only_first_name_is_not_an_application_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form action="/post-application">
          <label>First name <input name="first_name" required></label>
          <button type="submit">Apply now</button>
        </form>
        """
    )
    fields, readiness = GenericAdapter().inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False


def test_ats_marker_full_profile_continue_without_identity_binding_is_risk_four(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <main data-employer="Acme Capital">
          <form id="grnhse_app" class="greenhouse-form" action="/next">
            <input type="hidden" name="job_id" value="REQ-ATTACKER">
            <label>First name <input name="first_name"></label>
            <label>Last name <input name="last_name"></label>
            <label>Email <input name="email" type="email"></label>
            <label>University <input name="university"></label>
            <button type="button">Continue</button>
          </form>
        </main>
        """
    )
    adapter = GenericAdapter()
    fields, readiness = adapter.inspect_with_evidence(browser_page)
    plan = build_fill_plan(
        fields,
        DeterministicClassifier(),
        {},
        answer_lookup=lambda *_args: None,
        document_lookup=lambda *_args: None,
        adapter_name="greenhouse",
        step_evidence=readiness,
    )

    assert fields == []
    assert readiness["root_found"] is False
    assert plan.risk.level == 4
    assert "application_form_not_found" in plan.risk.blocking_codes


def test_forged_ats_contact_form_with_full_profile_and_submit_text_is_not_root(
    browser_page,
) -> None:
    """Mutable ATS-like labels and submit copy cannot self-attest an application."""

    browser_page.set_content(
        """
        <main>
          <form id="grnhse_app" class="greenhouse-form" action="/submit">
            <input name="first_name"><input name="last_name">
            <input name="email" type="email"><input name="phone">
            <input name="university">
            <button type="submit">Submit application</button>
          </form>
        </main>
        """
    )
    fields, readiness = GenericAdapter().inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False


def _externally_bound_resolution(page_url: str = "") -> TargetResolution:
    target_url = page_url or "http://127.0.0.1:8787/lab/ats/standard"
    return TargetResolution(
        source_url=target_url,
        final_url=target_url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "ats": "greenhouse",
            "employer": "Acme Capital",
            "role": "Summer Analyst",
            "requisition": "REQ-42",
            "form_identity": "application-form",
        },
    )


def _externally_bound_adapter(page=None) -> GenericAdapter:
    return AdapterRegistry().detect(
        _externally_bound_resolution(str(getattr(page, "url", "") or ""))
    )


def _mechanics_adapter(page=None) -> GenericAdapter:
    target_url = str(getattr(page, "url", "") or "") or "http://127.0.0.1:8787/lab/ats/standard"
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


def test_current_greenhouse_remix_form_is_bound_and_passive_recaptcha_is_not_a_boundary(
    browser_page,
) -> None:
    """Regression for the public Greenhouse React/Remix application surface."""

    url = "https://job-boards.greenhouse.io/point72/jobs/8423978002?gh_jid=8423978002"
    role = "2026 Warsaw MI Data - Web Scraping Internship"
    resolution = TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_FORM,
        provider="greenhouse",
        identity_verified=True,
        form_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Point72",
            "role": role,
            "requisition": "8423978002",
            # The resolver binds Remix forms to the posting ID, not the CSS ID.
            "form_identity": "8423978002",
        },
    )
    markup = f"""
      <!doctype html><html><body>
        <h1>{role}</h1>
        <form id="application-form">
          <label>First Name<input name="first_name" required></label>
          <label>Last Name<input name="last_name" required></label>
          <label>Email<input name="email" type="email" required></label>
          <div class="phone-country-picker">
            <input type="search" role="combobox" aria-label="Search">
          </div>
          <label>What is your preferred work location?
            <select name="preferred_location" required><option>Warsaw</option></select>
          </label>
          <div>
            <button type="button">Attach</button>
            <label for="resume">Attach</label>
            <input id="resume" type="file" required>
          </div>
          <button type="submit">Submit application</button>
        </form>
        <script>
          window.__remixContext = {{state: {{loaderData: {{
            root: {{boardConfiguration: {{disable_captcha: false}}}},
            "routes/$url_token_.jobs_.$job_post_id": {{
              jobPost: {{company_name: "Point72", title: "{role}"}},
              jobPostId: "8423978002"
            }}
          }}}}}};
        </script>
        <div class="grecaptcha-badge" style="display:block;width:70px;height:60px">
          <iframe title="recaptcha" src="about:blank" style="width:70px;height:60px"></iframe>
        </div>
        <textarea class="g-recaptcha-response" style="display:none"></textarea>
      </body></html>
    """

    page = browser_page
    original_url = page.url
    handler = lambda route: route.fulfill(status=200, content_type="text/html", body=markup)
    page.route(url, handler)
    try:
        page.goto(url)
        adapter = AdapterRegistry().detect(resolution)

        fields, readiness = adapter.inspect_with_evidence(page)
        boundary = adapter.detect_human_boundary(page)
    finally:
        page.unroute(url, handler)
        page.goto(original_url)

    assert resolution.verified_for_automation is True
    assert adapter.name == "greenhouse"
    assert readiness["page_root_found"] is True
    assert readiness["binding_verified"] is True
    assert readiness["root_found"] is True
    assert {field.question.name for field in fields} >= {
        "first_name",
        "last_name",
        "email",
        "preferred_location",
        "resume",
    }
    assert all(field.control_type != "captcha" for field in fields)
    assert boundary == (False, "")


def test_visible_interactive_captcha_remains_a_human_boundary(browser_page) -> None:
    browser_page.set_content(
        """
        <div class="g-recaptcha" data-sitekey="test-key"
             style="display:block;width:300px;height:80px">Verify you are human</div>
        """
    )

    adapter = AdapterRegistry().detect(_externally_bound_resolution(str(browser_page.url)))

    assert adapter.detect_human_boundary(browser_page) == (True, "captcha_detected")


def test_greenhouse_react_combobox_selects_a_visible_exact_option(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-ats-application data-role="Summer Analyst"
              action="/apply/REQ-COMBO">
          <label>First name<input name="first_name"></label>
          <label>Last name<input name="last_name"></label>
          <label>Email<input name="email" type="email"></label>
          <label id="work-auth-label">Are you legally authorized to work?</label>
          <div id="work-auth-select">
            <div class="select__single-value" id="selected-work-auth"></div>
            <input id="work_auth" type="text" role="combobox"
                   aria-labelledby="work-auth-label" aria-expanded="false">
            <div id="work-auth-options" hidden>
              <div role="option">Yes</div>
              <div role="option">No</div>
            </div>
          </div>
          <input id="resume" type="file">
          <button type="submit">Submit application</button>
        </form>
        <script>
          const input = document.getElementById('work_auth');
          const menu = document.getElementById('work-auth-options');
          input.addEventListener('click', () => { menu.hidden = false; input.setAttribute('aria-expanded', 'true'); });
          for (const option of menu.querySelectorAll('[role="option"]')) {
            option.addEventListener('click', () => {
              document.getElementById('selected-work-auth').textContent = option.textContent;
              input.value = '';
              input.setAttribute('aria-expanded', 'false');
              menu.hidden = true;
            });
          }
        </script>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    fields, _readiness = adapter.inspect_with_evidence(browser_page)
    field = next(item for item in fields if item.question.name == "work_auth")

    adapter.fill(browser_page, field, "Yes")

    assert field.control_type == "combobox"
    assert browser_page.locator("#selected-work-auth").inner_text() == "Yes"
    assert adapter.verify_step(
        browser_page,
        fields,
        {"work_auth": "Yes"},
    ) is True


@pytest.mark.parametrize(
    "markup",
    [
        """
        <form id="grnhse_app" class="greenhouse-form" data-role="Attacker Role" action="/submit">
          <input name="first_name"><input name="last_name"><input name="email">
          <button type="submit">Submit application</button>
        </form>
        """,
        """
        <form id="grnhse_app" class="greenhouse-form" data-requisition="REQ-ATTACKER" action="/submit">
          <input name="first_name"><input name="last_name"><input name="email">
          <button type="submit">Submit application</button>
        </form>
        """,
        """
        <form id="application-form" data-ats-application action="/jobs/ATTACKER/apply">
          <input name="first_name"><input name="last_name"><input name="email">
          <button type="submit">Submit application</button>
        </form>
        """,
        """
        <form id="grnhse_app" class="greenhouse-form" action="/submit">
          <input name="first_name"><input name="last_name"><input name="email">
          <input name="resume" type="file">
          <button type="submit">Submit application</button>
        </form>
        """,
    ],
)
def test_page_controlled_form_evidence_cannot_create_automation_root(
    browser_page, markup: str
) -> None:
    browser_page.set_content(markup)
    adapter = _externally_bound_adapter(browser_page)

    fields, readiness = adapter.inspect_with_evidence(browser_page)

    assert fields  # review can inspect the candidate controls
    assert readiness["root_found"] is False
    assert readiness["automation_ready"] is False
    assert adapter._last_form_handle is None


def test_externally_bound_role_requisition_and_form_identity_enable_root(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-ats-application
              data-argus-form-identity="application-form"
              data-employer="Acme Capital" data-role="Summer Analyst"
              data-requisition="REQ-42" action="/apply/REQ-42">
          <input name="first_name"><input name="last_name"><input name="email">
          <button type="submit">Submit application</button>
        </form>
        """
    )
    adapter = _externally_bound_adapter(browser_page)

    fields, readiness = adapter.inspect_with_evidence(browser_page)

    assert fields
    assert readiness["root_found"] is True
    assert readiness["automation_ready"] is True
    assert readiness["binding_verified"] is True
    assert adapter._last_form_handle is not None


@pytest.mark.parametrize(
    "page_url",
    [
        "about:blank",
        "data:text/html,<form></form>",
        "blob:https://127.0.0.1:8787/opaque",
        "file:///tmp/application.html",
        "javascript:document.body",
        "",
        "not a valid URL",
        "http://127.0.0.1:8788/lab/ats/standard",
    ],
)
def test_non_http_page_origins_never_become_automation_ready(page_url: str) -> None:
    raw = {
        "root_found": True,
        "root_description": "form#application-form",
        "root_selector": "#application-form",
        "root_token": "root-token",
        "submit_present": True,
        "fields": [{
            "selector": "#email",
            "label": "Email",
            "field_type": "email",
            "name": "email",
            "control_type": "email",
            "value_attribute": "",
            "required": True,
            "options": [],
        }],
        "root_identity": {
            "employer": "Acme Capital",
            "role": "Summer Analyst",
            "requisition": "REQ-42",
            "form_identity": "application-form",
            "provider": "greenhouse",
        },
    }
    adapter = _externally_bound_adapter(browser_page)

    fields, readiness = adapter.inspect_with_evidence(_FakePage(raw, page_url))

    assert fields
    assert readiness["page_root_found"] is True
    assert readiness["root_found"] is False
    assert readiness["automation_ready"] is False
    assert adapter._last_form_handle is None


@pytest.mark.parametrize(
    ("expected_url", "page_url", "should_bind"),
    [
        (
            "http://127.0.0.1:0/lab/ats/standard",
            "http://127.0.0.1:0/lab/ats/standard",
            False,
        ),
        (
            "https://127.0.0.1:0/lab/ats/standard",
            "https://127.0.0.1:0/lab/ats/standard",
            False,
        ),
        (
            "http://127.0.0.1/lab/ats/standard",
            "http://127.0.0.1:80/lab/ats/standard",
            True,
        ),
        (
            "http://127.0.0.1:80/lab/ats/standard",
            "http://127.0.0.1/lab/ats/standard",
            True,
        ),
        (
            "https://127.0.0.1/lab/ats/standard",
            "https://127.0.0.1:443/lab/ats/standard",
            True,
        ),
        (
            "https://127.0.0.1:443/lab/ats/standard",
            "https://127.0.0.1/lab/ats/standard",
            True,
        ),
        (
            "http://127.0.0.1/lab/ats/standard",
            "https://127.0.0.1/lab/ats/standard",
            False,
        ),
        (
            "https://127.0.0.1/lab/ats/standard",
            "https://127.0.0.1:8443/lab/ats/standard",
            False,
        ),
    ],
)
def test_effective_port_binding_rejects_zero_and_requires_exact_origin(
    expected_url: str, page_url: str, should_bind: bool
) -> None:
    raw = {
        "root_found": True,
        "root_description": "form#application-form",
        "root_selector": "#application-form",
        "root_token": "root-token",
        "submit_present": True,
        "fields": [{
            "selector": "#email",
            "label": "Email",
            "field_type": "email",
            "name": "email",
            "control_type": "email",
            "value_attribute": "",
            "required": True,
            "options": [],
        }],
        "root_identity": {
            "employer": "Acme Capital",
            "role": "Summer Analyst",
            "requisition": "REQ-42",
            "form_identity": "application-form",
            "provider": "greenhouse",
        },
    }
    adapter = AdapterRegistry().detect(_externally_bound_resolution(expected_url))

    _fields, readiness = adapter.inspect_with_evidence(_FakePage(raw, page_url))

    assert readiness["root_found"] is should_bind
    assert readiness["automation_ready"] is should_bind


def test_nested_submit_target_is_descendant_bound_to_verified_root(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" data-ats-application action="/apply/REQ-4">
          <div class="fields"><label>First name <input name="firstName"></label></div>
          <div class="actions"><span><button type="submit">Submit application</button></span></div>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    _fields, readiness = adapter.inspect_with_evidence(browser_page)
    handle = adapter._last_form_handle

    assert handle is not None
    assert readiness["root_selector"] == "#application-form"
    target = adapter.submission_target(browser_page, handle)

    assert target is not None
    assert "#application-form" in target.control_selector
    assert browser_page.locator(target.control_selector).count() == 1
    assert browser_page.locator(target.control_selector).inner_text() == "Submit application"


def test_submission_target_rejects_descendant_replacement_after_inspection(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" data-ats-application action="/apply/REQ-4b">
          <input name="firstName">
          <div class="actions"><span><button type="submit">Submit application</button></span></div>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    adapter.inspect_with_evidence(browser_page)
    handle = adapter._last_form_handle
    assert handle is not None

    browser_page.locator(".actions span").evaluate(
        "element => element.innerHTML = '<button type=submit>Submit application</button>'"
    )

    assert adapter.submission_target(browser_page, handle) is None


def test_submission_target_rejects_form_action_mutation_after_inspection(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" data-ats-application action="/apply/REQ-4c">
          <input name="firstName">
          <button type="submit">Submit application</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    adapter.inspect_with_evidence(browser_page)
    handle = adapter._last_form_handle
    assert handle is not None

    browser_page.locator("#application-form").evaluate(
        "form => form.setAttribute('action', '/collect-attacker')"
    )

    assert adapter.submission_target(browser_page, handle) is None


def test_submission_target_rejects_handle_from_another_frame_url(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" data-ats-application action="/apply/REQ-5">
          <input name="firstName"><button type="submit">Submit application</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    adapter.inspect_with_evidence(browser_page)
    handle = adapter._last_form_handle

    assert handle is not None
    from dataclasses import replace

    mismatched = replace(handle, frame_url="https://different-frame.example.test/form")
    assert adapter.submission_target(browser_page, mismatched) is None


def test_submission_target_rejects_handle_for_uninspected_root(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-ats-application action="/apply/REQ-5b">
          <input name="firstName"><button type="submit">Submit application</button>
        </form>
        <form id="newsletter" action="/subscribe">
          <input name="email"><button type="submit">Submit application</button>
        </form>
        """
    )
    adapter = GenericAdapter()
    adapter.inspect_with_evidence(browser_page)
    from app.automation.targets import FormHandle

    forged = FormHandle(
        page_id=str(id(browser_page)),
        frame_url=browser_page.url,
        root_selector="#newsletter",
        provider="generic",
        evidence={"root_found": True, "control_count": 1, "submit_present": True},
    )

    assert adapter.submission_target(browser_page, forged) is None


def test_submission_target_rejects_same_root_handle_without_verified_token(
    browser_page,
) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" data-ats-application action="/apply/REQ-5c">
          <input name="firstName"><button type="submit">Submit application</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    adapter.inspect_with_evidence(browser_page)
    inspected = adapter._last_form_handle

    assert inspected is not None
    from dataclasses import replace

    forged = replace(inspected, evidence={"root_found": True, "control_count": 1, "submit_present": True})
    assert adapter.submission_target(browser_page, forged) is None


def test_failed_inspection_clears_stale_form_handle(browser_page) -> None:
    browser_page.set_content(
        """
        <form id="application-form" data-role="Summer Analyst" data-ats-application action="/apply/REQ-6">
          <input name="firstName"><button type="submit">Submit application</button>
        </form>
        """
    )
    adapter = _mechanics_adapter(browser_page)
    adapter.inspect_with_evidence(browser_page)
    assert adapter._last_form_handle is not None

    browser_page.set_content("<form id=search role=search><input name=q></form>")
    fields, readiness = adapter.inspect_with_evidence(browser_page)

    assert fields == []
    assert readiness["root_found"] is False
    assert adapter._last_form_handle is None
