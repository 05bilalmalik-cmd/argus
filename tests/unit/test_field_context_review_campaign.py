"""Campaign 02 review follow-up — adjudicate Luna static concerns with real RED.

Parent reproduced two exact failures (bare-SOURCE identity, password
precedence); remaining Luna items get executed probes here. Every live probe
uses a real loopback HTTP page plus a verified TargetResolution and asserts
automation_ready (not merely page_root_found). No marker/trust widening, no
production guards touched, synthetic fixtures only.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import Page, sync_playwright

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.classifier import DeterministicClassifier
from app.automation.field_context import extract_field_context
from app.automation.targets import TargetResolution
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity
from app.domain.targets import TargetKind


CAMPAIGN_PATH = "/lab/review/campaign"
LOGIN_PATH = "/lab/review/login"

_CAMPAIGN_FORM = """
<main data-employer="Review Employer" data-role="Summer Analyst" data-requisition="REQ-REVIEW">
<form id="review-form" data-argus-form-identity="review-form" method="post" action="/submit">
  <h2>Review Application</h2>
  <div><label for="first_name">First name</label>
    <input type="text" id="first_name" name="first_name" /></div>
  <div><label for="last_name">Last name</label>
    <input type="text" id="last_name" name="last_name" /></div>
  <div><label for="email">Email</label>
    <input type="email" id="email" name="email" /></div>
  <div><label for="cv">CV upload</label>
    <input type="file" id="cv" name="cv" /></div>
  <fieldset id="fs-work">
    <legend>Do you currently have the legal right to work in the United Kingdom?</legend>
    <div><label for="work_details">Provide details of your work authorisation</label>
      <input type="text" id="work_details" name="work_details" /></div>
    <div><label for="country_box">United Kingdom</label>
      <input type="text" id="country_box" name="country_box" /></div>
    <div><label for="work_doc">Work authorisation document type</label>
      <select id="work_doc" name="work_doc">
        <option value="">Choose one</option>
        <option value="passport">Passport</option>
        <option value="visa">Visa</option>
      </select></div>
    <div><label><input type="checkbox" id="work_confirm" name="work_confirm" />
      I confirm the above work details are accurate</label></div>
    <div><label><input type="radio" name="work_radio" value="yes" /> Yes</label>
      <label><input type="radio" name="work_radio" value="no" /> No</label></div>
  </fieldset>
  <fieldset id="fs-education">
    <legend>Education history</legend>
    <div><label for="uni">University attended</label>
      <input type="text" id="uni" name="university" /></div>
  </fieldset>
  <fieldset id="fs-outer">
    <label for="outer">Ordinary field</label>
    <input type="text" id="outer" name="outer_field" />
    <fieldset id="fs-inner">
      <legend>Do you require visa sponsorship?</legend>
      <div><label><input type="radio" name="inner_sponsor" value="yes" /> Yes</label>
        <label><input type="radio" name="inner_sponsor" value="no" /> No</label></div>
    </fieldset>
  </fieldset>
  <div>
    <label for="ariawork">Work details</label>
    <span id="aria-legal">You must have the legal right to work in the United Kingdom</span>
    <input type="text" id="ariawork" name="ariawork" aria-labelledby="aria-legal" />
  </div>
  <div>
    <label for="extrainfo">Additional information</label>
    <span id="desc-legal">You must be legally authorised to work in the United Kingdom</span>
    <input type="text" id="extrainfo" name="extrainfo" aria-describedby="desc-legal" />
  </div>
  <div>
    <label for="wrongref">Preferred contact time</label>
    <input type="text" id="wrongref" name="wrongref" aria-labelledby="missing-id" />
  </div>
  <div>
    <label for="source">How did you hear about us?</label>
    <select id="source" name="source">
      <option value="">Select...</option>
      <option value="careers_page">Company Careers Page</option>
      <option value="job_board">Job Board (LinkedIn, Indeed, etc.)</option>
      <option value="referral">Employee Referral</option>
      <option value="other">Other</option>
    </select>
  </div>
  <div>
    <label for="linkedin">LinkedIn Profile URL</label>
    <input type="text" id="linkedin" name="linkedin" />
  </div>
  <button type="submit">Submit application</button>
</form>
</main>
"""

_LOGIN_FORM = """
<main data-employer="Review Employer" data-role="Summer Analyst" data-requisition="REQ-REVIEW">
<form id="login-form" method="post" action="/login">
  <h2>Returning candidates log in</h2>
  <div><label for="login_email">Email</label>
    <input type="email" id="login_email" name="email" /></div>
  <div><label for="login_pw">Password</label>
    <input type="password" id="login_pw" name="password" /></div>
  <button type="submit">Log in to continue application</button>
</form>
</main>
"""


@pytest.fixture(scope="module")
def review_server():
    pages = {CAMPAIGN_PATH: _CAMPAIGN_FORM, LOGIN_PATH: _LOGIN_FORM}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = pages.get(self.path, "<html><body>not found</body></html>").encode()
            self.send_response(200 if self.path in pages else 404)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _resolution(base_url: str, path: str, provider: str) -> TargetResolution:
    url = base_url + path
    return TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_FORM,
        provider=provider,
        identity_verified=True,
        form_verified=True,
        reason_codes=("review-campaign",),
        evidence={
            "provider": provider,
            "employer": "Review Employer",
            "role": "Summer Analyst",
            "requisition_id": "REQ-REVIEW",
            "form_id": "review-form",
            "synthetic_lab": True,
        },
    )


@pytest.fixture(scope="module")
def campaign_pages(review_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        generic_page = browser.new_page()
        generic_page.goto(review_server + CAMPAIGN_PATH)
        generic_page.wait_for_load_state("domcontentloaded")
        greenhouse_page = browser.new_page()
        greenhouse_page.goto(review_server + CAMPAIGN_PATH)
        greenhouse_page.wait_for_load_state("domcontentloaded")
        login_page = browser.new_page()
        login_page.goto(review_server + LOGIN_PATH)
        login_page.wait_for_load_state("domcontentloaded")
        yield {
            "base_url": review_server,
            "generic": generic_page,
            "greenhouse": greenhouse_page,
            "login": login_page,
        }
        browser.close()


def _bound_field(pages, adapter_name: str, field_name: str):
    page = pages["greenhouse"] if adapter_name == "greenhouse" else pages["generic"]
    provider = "greenhouse" if adapter_name == "greenhouse" else "generic"
    adapter = (
        GreenhouseAdapter(_resolution(pages["base_url"], CAMPAIGN_PATH, provider))
        if adapter_name == "greenhouse"
        else GenericAdapter(_resolution(pages["base_url"], CAMPAIGN_PATH, provider))
    )
    fields, evidence = adapter.inspect_with_evidence(page)
    assert evidence.get("automation_ready") is True, (
        f"Review fixture must be automation-ready, got {evidence}"
    )
    return next(f for f in fields if f.question.name == field_name)


# ---- Luna 1: bare SOURCE identity from primary label/name -------------------

def test_parent_bare_source_identity_maps_to_source() -> None:
    question = FormQuestion(
        label="Source — LinkedIn — Referral — Other",
        field_type="select",
        name="source",
        options=("LinkedIn", "Referral", "Other"),
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.SOURCE


def test_bare_source_label_with_nonstandard_name_maps_to_source() -> None:
    question = FormQuestion(
        label="Source",
        field_type="select",
        name="q_9",
        options=("LinkedIn", "Referral", "Other"),
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.SOURCE


def test_incidental_brand_option_does_not_create_linkedin_source_override() -> None:
    question = FormQuestion(
        label="LinkedIn Profile URL — Referral — Other",
        field_type="text",
        name="linkedin",
        options=("Referral", "Other"),
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.LINKEDIN


def test_live_source_select_maps_to_source_generic(campaign_pages) -> None:
    field = _bound_field(campaign_pages, "generic", "source")
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.SOURCE


def test_live_source_select_maps_to_source_greenhouse(campaign_pages) -> None:
    field = _bound_field(campaign_pages, "greenhouse", "source")
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.SOURCE


def test_field_context_bridge_source_select_maps_to_source() -> None:
    doc = """
    <html><body><form>
      <label for="src">Source</label>
      <select id="src" name="source">
        <option>LinkedIn</option><option>Referral</option><option>Other</option>
      </select>
    </form></body></html>
    """
    ctx = extract_field_context(
        '<select id="src" name="source"></select>', doc, selector="#src"
    )
    mapping = DeterministicClassifier().classify(ctx.to_form_question())
    assert mapping.canonical_key == CanonicalKey.SOURCE


# ---- Luna 2: password type precedence ---------------------------------------

def test_parent_password_source_label_maps_to_account_password() -> None:
    question = FormQuestion(
        label="How did you hear about us?",
        field_type="password",
        name="password",
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.ACCOUNT_PASSWORD
    assert mapping.sensitivity == Sensitivity.SENSITIVE


def test_password_type_beats_legal_keyword() -> None:
    question = FormQuestion(
        label="Do you have the legal right to work?",
        field_type="password",
        name="pw",
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.ACCOUNT_PASSWORD


def test_password_and_linkedin_controls_unchanged() -> None:
    classifier = DeterministicClassifier()
    assert (
        classifier.classify(FormQuestion("Password", "password")).canonical_key
        == CanonicalKey.ACCOUNT_PASSWORD
    )
    assert (
        classifier.classify(FormQuestion("LinkedIn Profile URL", "text")).canonical_key
        == CanonicalKey.LINKEDIN
    )
    assert (
        classifier.classify(FormQuestion("GitHub Profile URL", "text")).canonical_key
        == CanonicalKey.GITHUB
    )


def test_file_named_source_keeps_document_sensitivity() -> None:
    mapping = DeterministicClassifier().classify(
        FormQuestion("Source documents upload", "file", name="source_docs")
    )
    assert mapping.canonical_key != CanonicalKey.SOURCE


# ---- Luna 3: substring-overlap legend retained -------------------------------

def test_live_overlapping_own_label_keeps_sensitive_legend_generic(
    campaign_pages,
) -> None:
    field = _bound_field(campaign_pages, "generic", "country_box")
    assert "united kingdom" in field.question.label.casefold()
    assert "legal right to work" in field.question.label.casefold(), (
        f"Overlapping legend dropped: {field.question.label!r}"
    )
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.WORK_AUTHORISATION
    assert mapping.sensitivity == Sensitivity.LEGAL


def test_live_overlapping_own_label_keeps_sensitive_legend_greenhouse(
    campaign_pages,
) -> None:
    field = _bound_field(campaign_pages, "greenhouse", "country_box")
    assert "legal right to work" in field.question.label.casefold()


# ---- Luna 4: direct legend only + sibling help scoping -----------------------

def test_python_nested_fieldset_does_not_bleed() -> None:
    doc = """
    <form><fieldset>
      <label for="outer">Ordinary field</label>
      <input id="outer" name="outer">
      <fieldset><legend>Do you require visa sponsorship?</legend>
        <input name="inner">
      </fieldset>
    </fieldset></form>
    """
    ctx = extract_field_context(
        '<input id="outer" name="outer">', doc, selector="#outer"
    )
    assert ctx.fieldset_legend == ""


def test_live_outer_field_skips_nested_sponsorship_legend(campaign_pages) -> None:
    outer = _bound_field(campaign_pages, "generic", "outer_field")
    assert "visa sponsorship" not in outer.question.label.casefold()
    inner = _bound_field(campaign_pages, "generic", "inner_sponsor")
    assert "visa sponsorship" in inner.question.label.casefold()


def test_python_flat_sibling_hint_does_not_bleed() -> None:
    doc = """
    <form>
      <label for="a">Given name</label>
      <input id="a" name="a">
      <small class="hint">Enter your legal given name</small>
      <label for="b">University</label>
      <input id="b" name="b">
    </form>
    """
    ctx = extract_field_context('<input id="b" name="b">', doc, selector="#b")
    assert ctx.help_text == ""


# ---- Luna 5/6: competing ARIA + describedby ----------------------------------

def test_live_competing_aria_label_kept_with_visible_label_generic(
    campaign_pages,
) -> None:
    field = _bound_field(campaign_pages, "generic", "ariawork")
    assert "work details" in field.question.label.casefold()
    assert "legal right to work" in field.question.label.casefold(), (
        f"ARIA context lost: {field.question.label!r}"
    )
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.WORK_AUTHORISATION


def test_live_competing_aria_label_kept_greenhouse(campaign_pages) -> None:
    field = _bound_field(campaign_pages, "greenhouse", "ariawork")
    assert "work details" in field.question.label.casefold()
    assert "legal right to work" in field.question.label.casefold()


def test_python_bridge_keeps_aria_without_erasing_visible_label() -> None:
    doc = """
    <html><body><form>
      <label for="x">Work details</label>
      <span id="aria-legal">You must have the legal right to work</span>
      <input type="text" id="x" name="ariawork" aria-labelledby="aria-legal" />
    </form></body></html>
    """
    ctx = extract_field_context(
        '<input type="text" id="x" name="ariawork">', doc, selector="#x"
    )
    bridged = ctx.to_form_question()
    assert "work details" in bridged.label.casefold()
    assert "legal right to work" in bridged.label.casefold()


def test_live_describedby_kept_classifier_visible_without_erasing_label(
    campaign_pages,
) -> None:
    field = _bound_field(campaign_pages, "generic", "extrainfo")
    assert "additional information" in field.question.label.casefold()
    combined = f"{field.question.label} {field.question.placeholder}".casefold()
    assert "legally authorised" in combined, (
        f"describedby context lost: label={field.question.label!r} "
        f"placeholder={field.question.placeholder!r}"
    )
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.WORK_AUTHORISATION


def test_live_wrong_aria_ref_borrows_no_global_context(campaign_pages) -> None:
    field = _bound_field(campaign_pages, "generic", "wrongref")
    assert field.question.label == "Preferred contact time"


# ---- Login root stays excluded (negative control) ----------------------------

def test_login_form_with_password_is_not_an_application_root(campaign_pages) -> None:
    adapter = GenericAdapter(
        _resolution(campaign_pages["base_url"], LOGIN_PATH, "generic")
    )
    fields, evidence = adapter.inspect_with_evidence(campaign_pages["login"])
    assert fields == []
    assert evidence.get("page_root_found") is False
    assert evidence.get("automation_ready") is False
