"""Browser-level completeness regressions for form-scoped radio groups."""
import pytest
from playwright.sync_api import sync_playwright
from app.automation.runner import _OwnerThreadJourney
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion


@pytest.fixture(scope='module')
def page():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page()
        yield page
        browser.close()


@pytest.mark.parametrize(('checked','expected_blank'), [('yes',False),('no',False),('',True)])
def test_required_radio_checks_entire_scoped_group(page,checked,expected_blank):
    page.set_content('''<form id="target"><input type="radio" name="sponsor" value="yes" required>
      <input type="radio" name="sponsor" value="no" required></form>
      <form id="other"><input type="radio" name="sponsor" value="no" checked></form>''')
    if checked:
        page.locator(f'#target input[value="{checked}"]').check()
    field=InspectedField(selector='#target input[name="sponsor"]',control_type='radio',
        question=FormQuestion('Sponsorship','radio',name='sponsor',required=True))
    result=_OwnerThreadJourney._blank_required_fields(None,[field],page)
    assert bool(result) is expected_blank


def test_required_unchecked_checkbox_still_blocks(page):
    page.set_content('<form><input id="box" type="checkbox" required></form>')
    field=InspectedField(selector='#box',control_type='checkbox',
        question=FormQuestion('Attestation','checkbox',name='box',required=True))
    assert _OwnerThreadJourney._blank_required_fields(None,[field],page)==[('Attestation','required_field_blank')]
