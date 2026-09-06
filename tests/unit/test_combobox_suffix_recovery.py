"""Real owned-option recommits may render only a country dial-code suffix."""
import pytest
from playwright.sync_api import sync_playwright
from app.automation.adapters.greenhouse import GreenhouseAdapter, ComboboxResolutionError
from tests.unit.test_combobox_restore_integrity import _form

@pytest.mark.parametrize(('commit','rendered','valid'), [(True,'+1',True),(False,'+1',False),(True,'+44',False)])
def test_suffix_recovery_requires_matching_label_and_provider_commit(commit, rendered, valid):
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route: route.abort())
            field=_form(page,'','both')
            with pytest.raises(ComboboxResolutionError):
                GreenhouseAdapter().fill(page,field,'Approved College')
            page.evaluate('''({commit,rendered}) => {
                document.querySelector('#option').textContent='United States +1';
                document.querySelector('#submitted').value='US';
                document.querySelector('#selected').textContent=rendered;
                document.querySelector('#option').onclick=() => {
                    document.querySelector('#selected').textContent=rendered;
                    if (commit) document.querySelector('#submitted').value='US';
                };
            }''', {'commit':commit,'rendered':rendered})
            page.locator('#option').click()
            page.wait_for_timeout(75)
            assert page.locator('#application').evaluate('f => f.checkValidity()') is valid
            assert page.evaluate('window.submits')==0
        finally:
            browser.close()
