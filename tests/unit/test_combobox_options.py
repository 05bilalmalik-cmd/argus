from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, sync_playwright

from app.automation.adapters.greenhouse import GreenhouseAdapter


@pytest.fixture(scope="module")
def browser_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _field(page: Page, name: str):
    fields, _evidence = GreenhouseAdapter().inspect_with_evidence(page)
    return next(field for field in fields if field.question.name == name)


def _dynamic_combobox_form(
    *,
    question: str,
    options: tuple[str, ...],
    name: str = "work_authorisation",
) -> str:
    encoded_options = json.dumps(options)
    return f"""
    <form id="grnhse_app" class="greenhouse-form" data-role="Summer Analyst"
          action="/apply/REQ-COMBOBOX">
      <label>First name<input name="first_name"></label>
      <label>Last name<input name="last_name"></label>
      <label>Email<input name="email" type="email"></label>
      <label id="combobox-label">{question}</label>
      <div id="combobox-container">
        <div id="selected-option" class="select__single-value"></div>
        <input id="combobox" name="{name}" type="text" role="combobox"
               aria-labelledby="combobox-label" aria-controls="combobox-options"
               aria-expanded="false">
      </div>
      <button type="button">Submit application</button>
    </form>
    <script>
      const combo = document.getElementById('combobox');
      const optionTexts = {encoded_options};
      window.comboboxOpenCount = 0;
      window.submitCount = 0;
      document.getElementById('grnhse_app').addEventListener('submit', event => {{
        event.preventDefault();
        window.submitCount += 1;
      }});
      function closeOptions() {{
        document.getElementById('combobox-options')?.remove();
        combo.setAttribute('aria-expanded', 'false');
      }}
      function openOptions() {{
        if (document.getElementById('combobox-options')) return;
        window.comboboxOpenCount += 1;
        const listbox = document.createElement('div');
        listbox.id = 'combobox-options';
        listbox.setAttribute('role', 'listbox');
        for (const optionText of optionTexts) {{
          const option = document.createElement('div');
          option.setAttribute('role', 'option');
          option.textContent = optionText;
          option.addEventListener('click', () => {{
            document.getElementById('selected-option').textContent = optionText;
            combo.value = '';
            closeOptions();
          }});
          listbox.append(option);
        }}
        document.getElementById('combobox-container').append(listbox);
        combo.setAttribute('aria-expanded', 'true');
      }}
      combo.addEventListener('mousedown', openOptions);
      combo.addEventListener('keydown', event => {{
        if (event.key === 'ArrowDown') openOptions();
        if (event.key === 'Escape') closeOptions();
        if (event.key === 'Enter') event.preventDefault();
      }});
    </script>
    """


def test_aria_controls_combobox_enumerates_and_selects_long_affirmative_option(
    browser_page: Page,
) -> None:
    affirmative = (
        "Yes, I am legally authorized to work in the United Kingdom without sponsorship"  # synthetic fixture
    )
    negative = "No, I am not authorized to work in the United Kingdom"
    browser_page.set_content(
        _dynamic_combobox_form(
            question="Are you legally authorized to work in the United Kingdom?",
            options=(affirmative, negative),
        )
    )

    adapter = GreenhouseAdapter()
    field = _field(browser_page, "work_authorisation")

    assert field.control_type == "combobox"
    assert field.question.options == (affirmative, negative)
    assert browser_page.locator("#combobox").get_attribute("aria-expanded") == "false"

    adapter.fill(browser_page, field, "Yes")

    assert browser_page.locator("#selected-option").inner_text() == affirmative
    assert browser_page.evaluate("window.submitCount") == 0


def test_native_select_options_and_fill_are_unchanged(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <form id="grnhse_app" class="greenhouse-form" data-role="Summer Analyst"
              action="/apply/REQ-SELECT">
          <label>First name<input name="first_name"></label>
          <label>Last name<input name="last_name"></label>
          <label>Email<input name="email" type="email"></label>
          <label for="work_auth">Are you legally authorized to work?</label>
          <select id="work_auth" name="work_authorisation">
            <option value="">Choose one</option>
            <option value="yes">Yes</option>
            <option value="no">No</option>
          </select>
          <button type="button">Submit application</button>
        </form>
        """
    )

    adapter = GreenhouseAdapter()
    field = _field(browser_page, "work_authorisation")

    assert field.control_type == "select"
    assert field.question.options == ("Choose one", "Yes", "No")

    adapter.fill(browser_page, field, "No")

    assert browser_page.locator("#work_auth").input_value() == "no"


@pytest.mark.parametrize(
    ("stored_answer", "option"),
    [
        ("Yes", "No, I am not authorized to work in the United Kingdom"),
        ("No", "Yes, I am now authorized to work in the United Kingdom"),
    ],
)
def test_work_authorisation_inversion_never_matches(
    stored_answer: str,
    option: str,
) -> None:
    assert GreenhouseAdapter._match_combobox_option_index(
        stored_answer,
        (option,),
        question_label="Are you legally authorized to work?",
    ) is None


def test_equal_priority_matches_fail_closed() -> None:
    assert GreenhouseAdapter._match_combobox_option_index(
        "Yes",
        (
            "Yes, I am authorized to work in the United Kingdom",
            "Yes, I have the right to work in the United Kingdom",
        ),
        question_label="Are you authorized to work?",
    ) is None


def test_exact_match_takes_precedence_over_longer_prefix_match() -> None:
    assert GreenhouseAdapter._match_combobox_option_index(
        "  yEs  ",
        ("Yes, I am authorized to work in the United Kingdom", "YES"),
        question_label="Are you authorized to work?",
    ) == 1


def test_suffix_stripped_option_matches_country_with_dial_code() -> None:
    """Option labelled 'United Kingdom +44' matches stored 'United Kingdom'."""
    assert GreenhouseAdapter._match_combobox_option_index(
        "United Kingdom",
        ("United Kingdom +44",),
        question_label="Country*",
    ) == 0


def test_suffix_stripped_must_be_unique_fails_on_ambiguous() -> None:
    """Two different options that strip to the same value still raise."""
    assert GreenhouseAdapter._match_combobox_option_index(
        "United Kingdom",
        (
            "United Kingdom +44",
            "United Kingdom +353",
        ),
        question_label="Country*",
    ) is None


def test_suffix_stripped_no_matching_on_nonsensical_input() -> None:
    """No suffix-stripped match for a value absent from the options."""
    assert GreenhouseAdapter._match_combobox_option_index(
        "France",
        ("United Kingdom +44", "Germany +49"),
        question_label="Country*",
    ) is None


def test_suffix_stripped_does_not_break_polarity_matching() -> None:
    """Work-authorisation polarity is still enforced after suffix strip."""
    assert GreenhouseAdapter._match_combobox_option_index(
        "Yes",
        ("No, I am not authorized to work in the United Kingdom +44",),
        question_label="Are you legally authorized to work?",
    ) is None


def test_unenumerable_combobox_fails_closed_without_free_typing(browser_page: Page) -> None:
    browser_page.set_content(
        _dynamic_combobox_form(
            question="Are you legally authorized to work?",
            options=(),
        )
    )
    adapter = GreenhouseAdapter()
    field = _field(browser_page, "work_authorisation")

    assert field.question.options == ()

    with pytest.raises(RuntimeError, match="No unique approved option"):
        adapter.fill(browser_page, field, "Yes")

    assert browser_page.locator("#combobox").input_value() == ""
    assert browser_page.locator("#selected-option").inner_text() == ""
    assert browser_page.evaluate("window.submitCount") == 0


def test_sponsorship_inversion_never_matches_or_selects(browser_page: Page) -> None:
    browser_page.set_content(
        _dynamic_combobox_form(
            question="Will you now or in the future require sponsorship?",
            options=("Yes, I will require sponsorship",),
            name="sponsorship",
        )
    )
    adapter = GreenhouseAdapter()
    field = _field(browser_page, "sponsorship")

    with pytest.raises(RuntimeError, match="No unique approved option"):
        adapter.fill(browser_page, field, "No")

    assert browser_page.locator("#selected-option").inner_text() == ""
    assert browser_page.evaluate("window.submitCount") == 0


@pytest.mark.parametrize(
    ("stored_answer", "question", "options", "expected_index"),
    [
        (
            "I am legally authorised to work",
            "Are you legally authorized to work?",
            (
                "No, I am not authorized to work in the United Kingdom",
                "Yes, I am legally authorized to work in the United Kingdom",
            ),
            1,
        ),
        (
            "I do not require sponsorship",  # synthetic fixture
            "Will you now or in the future require sponsorship?",
            (
                "Yes, I will require sponsorship",
                "No, I will not require sponsorship",  # synthetic fixture
            ),
            1,
        ),
    ],
)
def test_explicit_work_authorisation_aliases_are_polarity_safe(
    stored_answer: str,
    question: str,
    options: tuple[str, ...],
    expected_index: int,
) -> None:
    assert GreenhouseAdapter._match_combobox_option_index(
        stored_answer,
        options,
        question_label=question,
    ) == expected_index


@pytest.mark.parametrize(
    ("combobox_attributes", "listbox_attributes", "option_attributes"),
    [
        ('aria-owns="owned-options"', 'id="owned-options"', ""),
        (
            'aria-activedescendant="active-yes"',
            'id="active-options"',
            'id="active-yes"',
        ),
        ("", 'id="associated-options" aria-labelledby="combobox-label"', ""),
    ],
)
def test_aria_owns_activedescendant_and_associated_listboxes_are_enumerated(
    browser_page: Page,
    combobox_attributes: str,
    listbox_attributes: str,
    option_attributes: str,
) -> None:
    browser_page.set_content(
        f"""
        <form id="grnhse_app" class="greenhouse-form" data-role="Summer Analyst"
              action="/apply/REQ-ARIA">
          <label>First name<input name="first_name"></label>
          <label>Last name<input name="last_name"></label>
          <label>Email<input name="email" type="email"></label>
          <label id="combobox-label">Are you legally authorized to work?</label>
          <div>
            <input id="work_auth" name="work_authorisation" role="combobox"
                   aria-labelledby="combobox-label" {combobox_attributes}>
            <div role="listbox" {listbox_attributes}>
              <div role="option" {option_attributes} aria-label="Yes">Ignored text</div>
              <div role="option">No</div>
            </div>
          </div>
          <button type="button">Submit application</button>
        </form>
        """
    )

    field = _field(browser_page, "work_authorisation")

    assert field.question.options == ("Yes", "No")
