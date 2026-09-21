"""Campaign 02 — field-context live extraction + SOURCE/LINKEDIN classification.

RED-first regression set for the two proven gaps:

1. Non-radio fieldset-legend gap: the live adapter's browser-side labelFor
   only returned <fieldset><legend> for radio/checkbox. Text/select inputs
   inside a fieldset lost the group question (including sensitive wording).
2. Branded SOURCE select contamination: a "How did you hear about us?"
   select whose options name LinkedIn was enriched with option text and the
   deterministic classifier matched LINKEDIN before SOURCE.

Positive controls lock behaviour that must NOT change: radio/checkbox
legends, real LinkedIn URL inputs (-> LINKEDIN), legal questions with brand
options (-> sensitive), aria-labelledby preservation, nearest-scope legend
(no cross-fieldset bleed), file-upload application-root evidence, and the
inherited Greenhouse live path (same inspect script via inheritance).

All fixtures are synthetic loopback-free set_content pages; no employer
network, no database, no arming.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, sync_playwright

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.classifier import DeterministicClassifier
from app.automation.field_context import extract_field_context
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity


_CAMPAIGN_FORM = """
<form id="campaign-form" method="post" action="/submit">
  <h2>Test Application</h2>
  <div>
    <label for="first_name">First name</label>
    <input type="text" id="first_name" name="first_name" />
  </div>
  <div>
    <label for="last_name">Last name</label>
    <input type="text" id="last_name" name="last_name" />
  </div>
  <div>
    <label for="email">Email</label>
    <input type="email" id="email" name="email" />
  </div>
  <div>
    <label for="cv">CV upload</label>
    <input type="file" id="cv" name="cv" />
  </div>
  <fieldset id="fs-work">
    <legend>Do you currently have the legal right to work in the United Kingdom?</legend>
    <div>
      <label for="work_details">Provide details of your work authorisation</label>
      <input type="text" id="work_details" name="work_details" />
    </div>
    <div>
      <label for="work_doc">Work authorisation document type</label>
      <select id="work_doc" name="work_doc">
        <option value="">Choose one</option>
        <option value="passport">Passport</option>
        <option value="visa">Visa</option>
      </select>
    </div>
    <div>
      <label><input type="checkbox" id="work_confirm" name="work_confirm" />
        I confirm the above work details are accurate</label>
    </div>
    <div>
      <label><input type="radio" name="work_radio" value="yes" /> Yes</label>
      <label><input type="radio" name="work_radio" value="no" /> No</label>
    </div>
  </fieldset>
  <fieldset id="fs-education">
    <legend>Education history</legend>
    <div>
      <label for="uni">University attended</label>
      <input type="text" id="uni" name="university" />
    </div>
  </fieldset>
  <div>
    <span id="aria-nickname-label">Preferred first name for correspondence</span>
    <input type="text" id="nickname" name="nickname" aria-labelledby="aria-nickname-label" />
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
    <input type="text" id="linkedin" name="linkedin" placeholder="https://linkedin.example/test-profile" />
  </div>
  <button type="submit">Submit application</button>
</form>
"""


@pytest.fixture(scope="module")
def browser_page() -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(_CAMPAIGN_FORM)
        yield page
        browser.close()


def _fields_by_name(page: Page, adapter, name: str):
    fields, evidence = adapter.inspect_with_evidence(page)
    assert evidence.get("page_root_found") is True, (
        "Campaign fixture must keep a positive application root; "
        f"evidence={evidence}"
    )
    return next(f for f in fields if f.question.name == name), evidence


def test_campaign_root_fixture_stays_positive(browser_page: Page) -> None:
    _, evidence = GenericAdapter().inspect_with_evidence(browser_page)
    assert evidence.get("page_root_found") is True
    assert any(
        f.question.field_type == "file"
        for f in GenericAdapter().inspect(browser_page)
    )


def test_live_text_input_in_fieldset_carries_legend(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "work_details")
    assert "legal right to work" in field.question.label.casefold(), (
        f"Non-radio legend missing from live label: {field.question.label!r}"
    )
    assert "details of your work authorisation" in field.question.label.casefold()


def test_live_select_in_fieldset_carries_legend(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "work_doc")
    assert "legal right to work" in field.question.label.casefold(), (
        f"Non-radio select legend missing from live label: {field.question.label!r}"
    )


def test_live_checkbox_in_fieldset_carries_legend(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "work_confirm")
    assert "legal right to work" in field.question.label.casefold(), (
        f"Checkbox legend missing from live label: {field.question.label!r}"
    )


def test_live_radio_in_fieldset_carries_legend(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "work_radio")
    assert "legal right to work" in field.question.label.casefold()


def test_legend_does_not_bleed_across_fieldsets(browser_page: Page) -> None:
    uni, _ = _fields_by_name(browser_page, GenericAdapter(), "university")
    assert "education history" in uni.question.label.casefold()
    assert "legal right to work" not in uni.question.label.casefold()
    details, _ = _fields_by_name(browser_page, GenericAdapter(), "work_details")
    assert "education history" not in details.question.label.casefold()


def test_aria_labelledby_text_is_preserved_not_replaced(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "nickname")
    assert "preferred first name for correspondence" in field.question.label.casefold(), (
        f"aria-labelledby text lost: {field.question.label!r}"
    )


def test_legitimate_label_not_replaced_by_option_value(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "work_details")
    assert field.question.label.strip().casefold() != "yes"
    assert "details of your work authorisation" in field.question.label.casefold()


def test_live_source_select_with_linkedin_option_maps_to_source(
    browser_page: Page,
) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "source")
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.SOURCE, (
        f"Branded option contaminated classification: got {mapping.canonical_key} "
        f"for label {field.question.label!r} ({mapping.reason})"
    )


def test_enriched_source_question_maps_to_source_not_linkedin() -> None:
    question = FormQuestion(
        label="How did you hear about us? — Job Board (LinkedIn, Indeed, etc.) — Employee Referral — Other",
        field_type="select",
        name="source",
        options=("Job Board (LinkedIn, Indeed, etc.)", "Employee Referral", "Other"),
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.SOURCE


def test_real_linkedin_url_input_still_maps_to_linkedin(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "linkedin")
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.LINKEDIN
    assert mapping.sensitivity == Sensitivity.STANDARD


def test_legal_question_with_brand_options_stays_sensitive() -> None:
    question = FormQuestion(
        label="Do you currently have the legal right to work in the United Kingdom? — LinkedIn — Yes — No",
        field_type="select",
        name="work_authorisation",
        options=("LinkedIn", "Yes", "No"),
    )
    mapping = DeterministicClassifier().classify(question)
    assert mapping.canonical_key == CanonicalKey.WORK_AUTHORISATION
    assert mapping.sensitivity == Sensitivity.LEGAL


def test_live_legal_radio_stays_sensitive(browser_page: Page) -> None:
    field, _ = _fields_by_name(browser_page, GenericAdapter(), "work_radio")
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key == CanonicalKey.WORK_AUTHORISATION
    assert mapping.sensitivity == Sensitivity.LEGAL


def test_greenhouse_inherits_legend_and_source_fixes(browser_page: Page) -> None:
    details, evidence = _fields_by_name(
        browser_page, GreenhouseAdapter(), "work_details"
    )
    assert evidence.get("page_root_found") is True
    assert "legal right to work" in details.question.label.casefold()
    source, _ = _fields_by_name(browser_page, GreenhouseAdapter(), "source")
    mapping = DeterministicClassifier().classify(source.question)
    assert mapping.canonical_key == CanonicalKey.SOURCE


def test_field_context_help_text_stays_field_scoped() -> None:
    doc = """
    <html><body><form>
      <div class="field-row">
        <label for="a">Given name</label>
        <input type="text" id="a" name="given_name" aria-describedby="hint-a" />
        <small id="hint-a" class="hint">Enter your legal given name</small>
      </div>
      <div class="field-row">
        <label for="b">University attended</label>
        <input type="text" id="b" name="university" />
      </div>
    </form></body></html>
    """
    uni = extract_field_context(
        '<input type="text" id="b" name="university">', doc,
        selector='input[name="university"]',
    )
    assert uni.help_text == ""
    assert "legal given name" not in uni.context_text.casefold()
