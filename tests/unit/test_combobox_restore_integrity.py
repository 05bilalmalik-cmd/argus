"""Synthetic-only provider state integrity and honest handoff regressions."""
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from app.automation.adapters.greenhouse import ComboboxResolutionError, GreenhouseAdapter
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        try:
            yield page
        finally:
            browser.close()


def _form(page, original, mutation):
    page.set_content('''<form id="application" onsubmit="event.preventDefault(); window.submits++">
      <div id="target-wrapper"><span id="selected" class="select__single-value"></span>
        <div><input id="field" role="combobox" aria-controls="options" aria-expanded="false"></div>
        <input type="hidden" id="submitted" name="school">
      </div>
      <div id="sibling-wrapper"><span class="select__single-value">Sibling College</span>
        <input id="sibling" role="combobox" aria-controls="sibling-options">
        <input type="hidden" name="sibling_school" value="sibling-id">
      </div><button id="submit" type="submit">Submit</button>
      </form>
      <div role="listbox" id="options"><div id="option" role="option">Approved College</div></div>
      <div role="listbox" id="sibling-options"><div id="sibling-option" role="option">Sibling College</div></div>
      <script>window.submits=0; window.enters=0;
        document.querySelector('#field').addEventListener('keydown', e => {if(e.key==='Enter') window.enters++});
      </script>''')
    page.evaluate('''({original, mutation}) => {
      document.querySelector('#selected').textContent = original;
      document.querySelector('#submitted').value = original ? 'original-id' : '';
      document.querySelector('#option').onclick = () => {
        document.querySelector('#submitted').value = 'wrong-id';
        if (mutation === 'both') document.querySelector('#selected').textContent = 'Wrong College';
      };
    }''', {"original": original, "mutation": mutation})
    return InspectedField(selector="#field", control_type="combobox",
                          question=FormQuestion("School", "text", name="school"))


@pytest.mark.parametrize("original", ["", "Original College"])
@pytest.mark.parametrize("mutation", ["both", "hidden_only"])
def test_failed_commit_preserves_truth_and_requires_explicit_reselection(page, original, mutation):
    field = _form(page, original, mutation)
    sibling_before = page.locator("#sibling-wrapper").inner_html()
    adapter = GreenhouseAdapter()
    with pytest.raises(ComboboxResolutionError) as error:
        adapter.fill(page, field, "Approved College")

    expected_label = "Wrong College" if mutation == "both" else original
    assert page.locator("#selected").inner_text() == expected_label, "Do not cosmetically erase provider rendering"
    assert page.evaluate("new FormData(document.querySelector('form')).get('school')") == "wrong-id"
    assert page.locator("#sibling-wrapper").inner_html() == sibling_before
    assert not page.locator("#application").evaluate("form => form.checkValidity()"), "Unverified provider state must block native submit"
    assert error.value.reason_code == "restoration_unverified"
    assert not adapter.verify_step(page, [field], {"school": "Approved College"})
    assert page.evaluate("window.enters") == 0
    assert page.evaluate("window.submits") == 0
    page.locator("#submit").click()
    assert page.evaluate("window.submits") == 0

    # Search text, unrelated options and untrusted events are not reselection.
    page.locator("#sibling-option").click()
    page.locator("#field").fill("Approved College")
    page.locator("#field").dispatch_event("change")
    assert not page.locator("#application").evaluate("form => form.checkValidity()")
    with pytest.raises(ComboboxResolutionError) as retry_error:
        adapter.fill(page, field, "Approved College")
    assert retry_error.value.reason_code == "restoration_unverified"

    # A rendered label alone still cannot resolve a changed hidden value.
    page.evaluate('''() => { document.querySelector('#option').onclick = () => {
      document.querySelector('#selected').textContent = 'Approved College';
    }; }''')
    page.locator("#option").click()
    page.wait_for_timeout(30)
    assert not page.locator("#application").evaluate("form => form.checkValidity()")
    assert not adapter.verify_step(page, [field], {"school": "Approved College"})

    # A trusted explicit option click whose rendered+submitted state commits
    # can release only this control's validity block.
    page.evaluate('''() => { document.querySelector('#option').onclick = () => {
      document.querySelector('#selected').textContent = 'Approved College';
      document.querySelector('#submitted').value = 'approved-id';
      document.querySelector('#field').value = '';
    }; }''')
    page.locator("#option").dispatch_event("click")
    assert not page.locator("#application").evaluate("form => form.checkValidity()")
    page.locator("#submitted").evaluate("element => element.value = 'wrong-id'")
    page.locator("#option").click()
    page.wait_for_function("document.querySelector('#application').checkValidity()")
    assert page.evaluate("new FormData(document.querySelector('form')).get('school')") == "approved-id"
    assert page.locator("#sibling-wrapper").inner_html() == sibling_before
    assert page.evaluate("window.submits") == 0


@pytest.mark.parametrize("reason_code", ["no_matching_option", "ambiguous_option", "commit_failed", "control_unavailable", "restoration_unverified"])
def test_runner_handoff_and_question_record_do_not_claim_unverified_blank(reason_code):
    from app.automation.runner import AutomationRunner, _OwnerThreadJourney
    from app.domain.questions import CanonicalKey, Sensitivity

    field = InspectedField(selector="#field", control_type="combobox",
                           question=FormQuestion("School", "text", name="school"))
    action = SimpleNamespace(field=field, status="resolved", value="Approved College", source="profile",
                             mapping=SimpleNamespace(canonical_key=CanonicalKey.UNIVERSITY,
                                                     confidence=1.0, sensitivity=Sensitivity.STANDARD))
    plan = SimpleNamespace(actions=[action])
    journey = SimpleNamespace(last_plan=plan, _safe_handoff_label=_OwnerThreadJourney._safe_handoff_label)
    details = _OwnerThreadJourney._pending_handoff_fields(journey, failed_field_reasons={"school": reason_code})
    assert details[0]["reason_code"] == reason_code
    assert "left blank" not in details[0]["reason"]
    if reason_code == "restoration_unverified":
        assert "reselect" in details[0]["reason"]
    records = []
    session = SimpleNamespace(add=records.append, flush=lambda: None)
    AutomationRunner._record_questions(session, SimpleNamespace(id="synthetic-run"), plan,
                                       failed_field_reasons={"school": reason_code})
    assert "left blank" not in records[0].reason
    assert records[0].mapping_status == "blocked"
    assert records[0].answer_preview == ""


def test_captcha_handoff_does_not_describe_unverified_selection_as_blank():
    from app.automation.runner import _handoff_next_action
    result = {"reason": "captcha", "manifest": {"blank_field_reasons": [
        {"label": "School", "reason": "selection unverified; reselect an option", "reason_code": "restoration_unverified"}
    ]}}
    assert "left blank" not in _handoff_next_action(result).casefold()

