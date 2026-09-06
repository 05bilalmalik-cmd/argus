"""Synthetic audit probes. These assert required behaviour, not expected bugs.
No network, real documents, profiles or application submissions are used.
Run with ARGUS venv from repo using pytest on this absolute file.
"""
import pytest
from playwright.sync_api import sync_playwright
from app.automation.adapters.greenhouse import GreenhouseAdapter, ComboboxResolutionError
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion
from tests.unit.test_combobox_restore_integrity import _form

@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(1500)
        page.route('**/*', lambda route: route.abort())
        try:
            yield page
        finally:
            browser.close()


def test_replacement_input_does_not_remove_unverified_submit_guard(page):
    field = _form(page, '', 'both')
    adapter = GreenhouseAdapter()
    with pytest.raises(ComboboxResolutionError):
        adapter.fill(page, field, 'Approved College')
    assert not page.locator('#application').evaluate('f => f.checkValidity()')
    page.locator('#field').evaluate('el => el.replaceWith(el.cloneNode(true))')
    page.locator('#submit').click()
    assert page.evaluate('window.submits') == 0, 'Input rerender must not unlock unverified native submission'


def test_same_input_country_change_invalidates_previous_dialcode_proof(page):
    page.set_content('''<form><div><span class="select__single-value"></span>
      <input id="field" role="combobox" aria-controls="opts">
      <input type="hidden" id="submitted" name="country">
      <div id="opts" role="listbox">
        <div id="us" role="option" onclick="document.querySelector('.select__single-value').textContent='+1'; document.querySelector('#submitted').value='US'">United States +1</div>
        <div id="ca" role="option" onclick="document.querySelector('.select__single-value').textContent='+1'; document.querySelector('#submitted').value='CA'">Canada +1</div>
      </div></div></form>''')
    field = InspectedField(selector='#field', control_type='combobox',
                           question=FormQuestion('Country', 'text', name='country'))
    adapter = GreenhouseAdapter()
    adapter.fill(page, field, 'United States')
    assert adapter.verify_step(page, [field], {'country': 'United States'})
    page.locator('#ca').click()
    assert page.locator('#submitted').input_value() == 'CA'
    assert not adapter.verify_step(page, [field], {'country': 'United States'}), 'Cached +1 selection cannot prove country after provider mutation'


def test_legitimate_recommit_of_already_correct_hidden_id_can_recover(page):
    field = _form(page, '', 'both')
    adapter = GreenhouseAdapter()
    with pytest.raises(ComboboxResolutionError):
        adapter.fill(page, field, 'Approved College')
    page.evaluate('''() => {
      document.querySelector('#submitted').value='approved-id';
      document.querySelector('#option').onclick=() => {
        document.querySelector('#selected').textContent='Approved College';
        document.querySelector('#submitted').value='approved-id';
      };
    }''')
    page.locator('#option').click()
    page.wait_for_timeout(100)
    assert page.locator('#application').evaluate('f => f.checkValidity()'), 'Real provider recommit must recover without requiring the correct ID to change'


@pytest.mark.parametrize("replacement", ["before", "during"])
def test_rerender_guard_allows_real_reselection(page, replacement):
    field = _form(page, '', 'both')
    adapter = GreenhouseAdapter()
    with pytest.raises(ComboboxResolutionError):
        adapter.fill(page, field, 'Approved College')
    if replacement == 'before':
        page.locator('#field').evaluate('el => el.replaceWith(el.cloneNode(true))')
    assert not adapter.verify_step(page, [field], {'school': 'Approved College'})
    page.evaluate("""replacement => {
      document.querySelector('#option').onclick = () => {
        if (replacement === 'during') {
          const old = document.querySelector('#field'); old.replaceWith(old.cloneNode(true));
        }
        document.querySelector('#selected').textContent = 'Approved College';
        document.querySelector('#submitted').value = 'approved-id';
      };
    }""", replacement)
    page.locator('#option').click()
    page.wait_for_function("document.querySelector('#application').checkValidity()")
    assert adapter.verify_step(page, [field], {'school': 'Approved College'})
    assert page.evaluate('window.submits') == 0


@pytest.mark.parametrize("action", ['noop', 'label_only', 'sibling', 'synthetic'])
def test_stale_or_unrelated_click_cannot_release_guard(page, action):
    field = _form(page, '', 'both')
    adapter = GreenhouseAdapter()
    with pytest.raises(ComboboxResolutionError):
        adapter.fill(page, field, 'Approved College')
    page.evaluate("""action => {
      document.querySelector('#submitted').value = 'approved-id';
      const commit = () => {
        document.querySelector('#selected').textContent = 'Approved College';
        document.querySelector('#submitted').value = 'approved-id';
      };
      document.querySelector('#option').onclick = action === 'synthetic' ? commit : () => {};
      if (action === 'noop') document.querySelector('#selected').textContent = 'Approved College';
      if (action === 'label_only') document.querySelector('#option').onclick = () => {
        document.querySelector('#selected').textContent = 'Approved College';
      };
      document.querySelector('#sibling-option').textContent = 'Approved College';
      document.querySelector('#sibling-option').onclick = commit;
      document.querySelector('#field').setAttribute('data-argus-committed-option', 'Approved College');
    }""", action)
    if action == 'synthetic':
        page.locator('#option').dispatch_event('click')
    else:
        page.locator('#sibling-option' if action == 'sibling' else '#option').click()
    page.wait_for_timeout(50)
    assert not page.locator('#application').evaluate('f => f.checkValidity()')
    assert not adapter.verify_step(page, [field], {'school': 'Approved College'})
    page.locator('#submit').click()
    assert page.evaluate('window.submits') == 0


@pytest.mark.parametrize("target", ['#field', '#submitted', '.select__single-value'])
def test_cached_suffix_proof_rejects_replacement_nodes(page, target):
    page.set_content("""<form><div><span class="select__single-value"></span>
      <input id="field" role="combobox" aria-controls="opts">
      <input id="submitted" type="hidden" name="country">
      <div id="opts" role="listbox"><div role="option" onclick="document.querySelector('.select__single-value').textContent='+1'; document.querySelector('#submitted').value='US'">United States +1</div></div>
      </div></form>""")
    field = InspectedField(selector='#field', control_type='combobox',
                           question=FormQuestion('Country', 'text', name='country'))
    adapter = GreenhouseAdapter()
    adapter.fill(page, field, 'United States')
    assert adapter.verify_step(page, [field], {'country': 'United States'})
    page.locator(target).evaluate('el => el.replaceWith(el.cloneNode(true))')
    assert not adapter.verify_step(page, [field], {'country': 'United States'})


def test_suffix_label_and_page_authored_marker_are_not_proof(page):
    page.set_content("""<form><div><span class="select__single-value">+1</span>
      <input id="field" role="combobox" data-argus-committed-option="United States +1">
      <input type="hidden" name="country" value="CA"></div></form>""")
    field = InspectedField(selector='#field', control_type='combobox',
                           question=FormQuestion('Country', 'text', name='country'))
    assert not GreenhouseAdapter().verify_step(page, [field], {'country': 'United States'})


def test_stale_suffix_noop_option_click_cannot_create_proof(page):
    page.set_content("""<form><div><span class="select__single-value">+1</span>
      <input id="field" role="combobox" aria-controls="opts">
      <input type="hidden" name="country" value="CA">
      <div id="opts" role="listbox"><div role="option">United States +1</div></div>
      </div></form>""")
    field = InspectedField(selector='#field', control_type='combobox',
                           question=FormQuestion('Country', 'text', name='country'))
    adapter = GreenhouseAdapter()
    with pytest.raises(ComboboxResolutionError):
        adapter.fill(page, field, 'United States')
    assert not adapter.verify_step(page, [field], {'country': 'United States'})
