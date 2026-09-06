from __future__ import annotations

from types import SimpleNamespace

import pytest
from playwright.sync_api import Page, sync_playwright

from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.runner import _active_handoff_result
from app.automation.adapters.workday import WorkdayAdapter
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion
from app.services.navigator import SessionState


@pytest.fixture(scope="module")
def browser_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _field(
    label: str,
    name: str,
    *,
    selector: str,
    field_type: str = "text",
    control_type: str | None = None,
    options: tuple[str, ...] = (),
) -> InspectedField:
    return InspectedField(
        selector=selector,
        control_type=control_type or field_type,
        question=FormQuestion(
            label=label,
            name=name,
            field_type=field_type,
            options=options,
        ),
    )


def test_native_select_label_verifies_when_dom_value_is_provider_code(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <form>
          <label for="country">Country</label>
          <select id="country" name="country">
            <option value="+44" selected>United Kingdom</option>
          </select>
        </form>
        """
    )
    field = _field(
        "Country",
        "country",
        selector="#country",
        field_type="select",
        control_type="select",
        options=("United Kingdom",),
    )

    assert GreenhouseAdapter().verify_step(
        browser_page,
        [field],
        {"country": "United Kingdom"},
    ) is True


def test_date_reformatted_on_blur_verifies_semantically(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <form>
          <label for="start">Start date</label>
          <input id="start" name="start_date" value="30/06/2027">
        </form>
        """
    )
    field = _field("Start date", "start_date", selector="#start")

    assert GreenhouseAdapter().verify_step(
        browser_page,
        [field],
        {"start_date": "2027-06-30"},
    ) is True


def test_event_driven_controlled_combobox_survives_user_style_selection(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <form>
          <label for="degree">Degree</label>
          <div>
            <span id="selected" class="select__single-value"></span>
            <input id="degree" name="degree" role="combobox"
                   aria-controls="degree-options" aria-expanded="false">
          </div>
        </form>
        <script>
          (() => {
          const input = document.querySelector('#degree');
          input.addEventListener('mousedown', () => {
            const list = document.createElement('div');
            list.id = 'degree-options';
            list.setAttribute('role', 'listbox');
            const option = document.createElement('div');
            option.setAttribute('role', 'option');
            option.textContent = 'Finance';
            option.addEventListener('click', () => {
              document.querySelector('#selected').textContent = 'Finance';
              input.value = '';
              list.remove();
            });
            list.append(option);
            document.body.append(list);
            input.setAttribute('aria-expanded', 'true');
          });
          input.addEventListener('input', () => { input.value = ''; });
          })();
        </script>
        """
    )
    field = _field(
        "Degree",
        "degree",
        selector="#degree",
        control_type="combobox",
    )
    adapter = GreenhouseAdapter()

    adapter.fill(browser_page, field, "Finance")

    assert browser_page.locator("#selected").inner_text() == "Finance"
    assert adapter.verify_step(browser_page, [field], {"degree": "Finance"}) is True


def test_required_controlled_combobox_uses_selected_label_for_validity(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <form>
          <label for="degree">Degree</label>
          <div>
            <span id="selected" class="select__single-value">Finance</span>
            <input id="degree" name="degree" role="combobox"
                   aria-required="true" required value="">
          </div>
        </form>
        """
    )
    field = _field(
        "Degree*",
        "degree",
        selector="#degree",
        control_type="combobox",
    )

    assert GreenhouseAdapter().verify_step(
        browser_page,
        [field],
        {"degree": "Finance"},
    ) is True


def test_file_prefill_fails_closed_without_provider_attachment_readback(
    browser_page: Page,
    tmp_path,
) -> None:
    browser_page.set_content(
        """
        <form>
          <div class="file-upload" role="group">
            <input id="resume" type="file">
          </div>
        </form>
        """
    )
    document = tmp_path / "approved-cv.txt"
    document.write_text("synthetic approved document", encoding="utf-8")
    field = _field(
        "Resume/CV*",
        "resume",
        selector="#resume",
        field_type="file",
        control_type="file",
    )

    with pytest.raises(RuntimeError, match="attachment"):
        GreenhouseAdapter().fill(browser_page, field, str(document))


def test_file_prefill_accepts_matching_provider_attachment_readback(
    browser_page: Page,
    tmp_path,
) -> None:
    browser_page.set_content(
        """
        <form>
          <div class="file-upload" role="group">
            <input id="resume" type="file">
            <span class="file-upload__filename" style="display: none"></span>
          </div>
        </form>
        <script>
          (() => {
            const input = document.querySelector('#resume');
            const filename = document.querySelector('.file-upload__filename');
            input.addEventListener('change', () => {
              filename.textContent = input.files[0]?.name || '';
              filename.style.display = 'inline-block';
            });
          })();
        </script>
        """
    )
    document = tmp_path / "approved-cv.txt"
    document.write_text("synthetic approved document", encoding="utf-8")
    field = _field(
        "Resume/CV*",
        "resume",
        selector="#resume",
        field_type="file",
        control_type="file",
    )

    GreenhouseAdapter().fill(browser_page, field, str(document))

    assert browser_page.locator(".file-upload__filename").inner_text() == document.name


def test_file_prefill_accepts_filename_child_with_provider_remove_affordance(
    browser_page: Page,
    tmp_path,
) -> None:
    browser_page.set_content(
        """
        <form>
          <div class="file-upload" role="group">
            <input id="resume" type="file">
            <span class="file-upload__filename" style="display: none">
              <span class="file-upload__name"></span>
              <button type="button" aria-label="Remove file">×</button>
            </span>
          </div>
        </form>
        <script>
          (() => {
            const input = document.querySelector('#resume');
            const filename = document.querySelector('.file-upload__filename');
            const name = document.querySelector('.file-upload__name');
            input.addEventListener('change', () => {
              name.textContent = input.files[0]?.name || '';
              filename.style.display = 'inline-block';
            });
          })();
        </script>
        """
    )
    document = tmp_path / "approved-cv.txt"
    document.write_text("synthetic approved document", encoding="utf-8")
    field = _field(
        "Resume/CV*",
        "resume",
        selector="#resume",
        field_type="file",
        control_type="file",
    )

    GreenhouseAdapter().fill(browser_page, field, str(document))

    assert browser_page.locator(".file-upload__name").inner_text() == document.name


def test_file_prefill_accepts_provider_readback_after_react_replaces_input(
    browser_page: Page,
    tmp_path,
) -> None:
    browser_page.set_content(
        """
        <form>
          <div class="file-upload" role="group">
            <input id="resume" type="file">
            <span class="file-upload__filename" style="display: none"></span>
          </div>
        </form>
        <script>
          (() => {
            const input = document.querySelector('#resume');
            const filename = document.querySelector('.file-upload__filename');
            input.addEventListener('change', () => {
              filename.textContent = input.files[0]?.name || '';
              filename.style.display = 'inline-block';
              input.value = '';
              input.remove();
            });
          })();
        </script>
        """
    )
    document = tmp_path / "approved-cv.txt"
    document.write_text("synthetic approved document", encoding="utf-8")
    field = _field(
        "Resume/CV*",
        "resume",
        selector="#resume",
        field_type="file",
        control_type="file",
    )

    GreenhouseAdapter().fill(browser_page, field, str(document))

    assert browser_page.locator(".file-upload__filename").inner_text() == document.name


def test_searchable_typeahead_requires_and_selects_an_enumerated_option(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <form>
          <label for="school">School</label>
          <div>
            <span id="selected" class="select__single-value"></span>
            <input id="school" name="school--0" role="combobox"
                   aria-expanded="false" aria-controls="options">
          </div>
        </form>
        <script>
          (() => {
          const input = document.querySelector('#school');
          input.addEventListener('input', () => {
            document.querySelector('#options')?.remove();
            const list = document.createElement('div');
            list.id = 'options';
            list.setAttribute('role', 'listbox');
            const option = document.createElement('div');
            option.setAttribute('role', 'option');
            option.textContent = input.value;
            option.addEventListener('click', () => {
              document.querySelector('#selected').textContent = input.value;
              input.value = '';
              list.remove();
            });
            list.append(option);
            document.body.append(list);
            input.setAttribute('aria-expanded', 'true');
          });
          })();
        </script>
        """
    )
    field = _field(
        "School",
        "school--0",
        selector="#school",
        control_type="combobox",
    )
    adapter = GreenhouseAdapter()

    adapter.fill(browser_page, field, "Northbridge College")

    assert browser_page.locator("#selected").inner_text() == "Northbridge College"
    assert adapter.verify_step(browser_page, [field], {"school--0": "Northbridge College"}) is True


def test_genuine_readback_mismatch_fails_closed(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <form>
          <label for="degree">Degree</label>
          <input id="degree" name="degree" value="Economics">
        </form>
        """
    )
    field = _field("Degree", "degree", selector="#degree")

    assert GreenhouseAdapter().verify_step(
        browser_page,
        [field],
        {"degree": "Finance"},
    ) is False


def test_workday_native_select_uses_rendered_option_label(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <div class="wd-step">
          <form>
            <label for="country">Country</label>
            <select id="country" name="country">
              <option value="GB" selected>United Kingdom</option>
            </select>
          </form>
        </div>
        """
    )
    field = _field(
        "Country",
        "country",
        selector="#country",
        field_type="select",
        control_type="select",
        options=("United Kingdom",),
    )

    assert WorkdayAdapter().verify_step(
        browser_page,
        [field],
        {"country": "United Kingdom"},
    ) is True


def test_active_headed_handoff_survives_runner_wait_timeout() -> None:
    snapshot = SimpleNamespace(
        state=SessionState.ACTIVE,
        worker_alive=True,
        human_boundary={},
    )

    result = _active_handoff_result(snapshot, headed=True)

    assert result == {
        "state": "NEEDS_USER",
        "reason": "Headed browser handoff remains active after the request wait",
        "risk_level": 3,
        "blocked_reasons": ("human_boundary", "handoff_pending"),
        "human_boundary": {},
    }


def test_terminal_or_headless_timeout_does_not_become_handoff() -> None:
    terminal = SimpleNamespace(
        state=SessionState.FAILED,
        worker_alive=False,
        human_boundary={},
    )
    active_headless = SimpleNamespace(
        state=SessionState.ACTIVE,
        worker_alive=True,
        human_boundary={},
    )

    assert _active_handoff_result(terminal, headed=True) is None
    assert _active_handoff_result(active_headless, headed=False) is None
