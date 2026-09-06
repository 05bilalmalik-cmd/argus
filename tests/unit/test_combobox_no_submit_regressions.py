"""No-submit and honest-readback browser regressions for comboboxes."""
import pytest
from playwright.sync_api import sync_playwright
from app.automation.adapters.greenhouse import GreenhouseAdapter, ComboboxResolutionError
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(1000)
        page.route('**/*', lambda route: route.abort())
        yield page
        browser.close()


def test_unmatched_combobox_never_presses_enter_or_submits(page):
    page.set_content('''<form onsubmit="event.preventDefault(); window.submits++">
      <input id="field" role="combobox" aria-controls="opts">
      <div id="opts" role="listbox"><div role="option">Economics</div></div>
      <button type="submit">Submit</button></form>
      <script>window.submits=0; window.enters=0; document.querySelector('#field').addEventListener('keydown', e => {if(e.key==='Enter') window.enters++});</script>''')
    field = InspectedField(selector='#field', control_type='combobox',
        question=FormQuestion('School', 'text', name='school'))
    with pytest.raises(ComboboxResolutionError) as error:
        GreenhouseAdapter().fill(page, field, 'Northbridge College')
    assert error.value.reason_code == 'no_matching_option'
    assert page.evaluate('window.enters') == 0
    assert page.evaluate('window.submits') == 0
    assert page.locator('#field').input_value() == ''


def test_readback_never_borrows_sibling_committed_value(page):
    page.set_content('''<form><div><span class="select__single-value">Finance</span>
      <input id="first" role="combobox"></div>
      <div><input id="second" role="combobox"></div></form>''')
    assert GreenhouseAdapter._selected_combobox_value(page.locator('#second')) == ''


def test_matched_dial_code_verifies_only_for_same_control_and_country(page):
    page.set_content('''<form><div><span class="select__single-value"></span>
      <input id="field" role="combobox" aria-controls="opts">
      <div id="opts" role="listbox"><div role="option" onclick="document.querySelector('.select__single-value').textContent='+44'">United Kingdom +44</div></div>
      </div></form>''')
    field = InspectedField(selector='#field', control_type='combobox',
        question=FormQuestion('Country', 'text', name='country'))
    adapter = GreenhouseAdapter()
    adapter.fill(page, field, 'United Kingdom')
    assert adapter.verify_step(page, [field], {'country':'United Kingdom'})
    assert not adapter.verify_step(page, [field], {'country':'France'})
    page.locator('.select__single-value').evaluate("el => el.textContent='+33'")
    assert not adapter.verify_step(page, [field], {'country':'United Kingdom'})


def test_unrelated_dial_code_does_not_verify_any_answer():
    assert not GreenhouseAdapter._combobox_value_matches('France', '+44', question_label='Country')
    assert not GreenhouseAdapter._combobox_value_matches('Finance', '+44', question_label='Degree')
