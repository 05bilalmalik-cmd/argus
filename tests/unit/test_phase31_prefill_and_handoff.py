from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Page, sync_playwright

from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.classifier import DeterministicClassifier
from app.automation.runner import build_fill_plan
from app.automation.types import InspectedField
from app.config import Settings
from app.domain.questions import CanonicalKey, FormQuestion
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, AutomationRun, Opportunity, QuestionRecord


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
    selector: str = "#field",
    field_type: str = "text",
    control_type: str | None = None,
    required: bool = True,
    options: tuple[str, ...] = (),
) -> InspectedField:
    return InspectedField(
        selector=selector,
        control_type=control_type or field_type,
        question=FormQuestion(
            label=label,
            name=name,
            field_type=field_type,
            required=required,
            options=options,
        ),
    )


def _combobox_form(*, options: tuple[str, ...], commit_script: str) -> str:
    option_json = repr(list(options)).replace("'", '"')
    return f"""
    <form id="application-form">
      <label for="field">Education field</label>
      <div id="wrapper">
        <span id="selected" class="select__single-value"></span>
        <input id="field" name="education--0" role="combobox"
               aria-controls="options" aria-expanded="false">
      </div>
    </form>
    <script>
      var input = document.querySelector('#field');
      var values = {option_json};
      input.addEventListener('input', () => {{
        document.querySelector('#options')?.remove();
        const list = document.createElement('div');
        list.id = 'options';
        list.setAttribute('role', 'listbox');
        values.forEach(value => {{
          const option = document.createElement('div');
          option.setAttribute('role', 'option');
          option.textContent = value;
          option.addEventListener('click', () => {{ {commit_script} }});
          list.append(option);
        }});
        document.body.append(list);
        input.setAttribute('aria-expanded', 'true');
      }});
    </script>
    """


def test_typeahead_selects_only_an_option_offered_by_the_widget(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        _combobox_form(
            options=("Computer Science and Engineering", "Economics"),
            commit_script="document.querySelector('#selected').textContent = event.currentTarget.textContent; document.querySelector('#options').remove(); input.value = ''; input.setAttribute('aria-expanded', 'false');",
        )
    )
    field = _field("Discipline", "education--0", control_type="combobox")

    GreenhouseAdapter().fill(browser_page, field, "Computer Science & Engineering")

    assert browser_page.locator("#selected").inner_text() == "Computer Science and Engineering"


def test_typeahead_absent_option_is_blank_with_no_matching_option(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        _combobox_form(
            options=("Economics",),
            commit_script="document.querySelector('#options').remove(); input.value = '';",
        )
    )
    field = _field("School", "school--0", control_type="combobox")

    with pytest.raises(RuntimeError) as error:
        GreenhouseAdapter().fill(browser_page, field, "Northbridge College")

    assert error.value.reason_code == "no_matching_option"
    assert browser_page.locator("#selected").inner_text() == ""
    assert browser_page.locator("#field").input_value() == ""


def test_typeahead_equally_good_options_are_blank_with_ambiguous_option(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        _combobox_form(
            options=("St Andrews University", "St Andrews College"),
            commit_script="document.querySelector('#selected').textContent = event.currentTarget.textContent;",
        )
    )
    field = _field("School", "school--0", control_type="combobox")

    with pytest.raises(RuntimeError) as error:
        GreenhouseAdapter().fill(browser_page, field, "St Andrews")

    assert error.value.reason_code == "ambiguous_option"
    assert browser_page.locator("#selected").inner_text() == ""


def test_reformatted_commit_is_verified_by_normalized_readback(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        _combobox_form(
            options=("Computer Science and Engineering",),
            commit_script="document.querySelector('#selected').textContent = 'Computer Science & Engineering'; document.querySelector('#options').remove(); input.value = ''; input.setAttribute('aria-expanded', 'false');",
        )
    )
    field = _field("Discipline", "education--0", control_type="combobox")
    adapter = GreenhouseAdapter()

    adapter.fill(browser_page, field, "Computer Science and Engineering")

    assert adapter.verify_step(
        browser_page,
        [field],
        {"education--0": "Computer Science & Engineering"},
    ) is True


def test_genuine_combobox_commit_failure_is_not_reported_as_success(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        _combobox_form(
            options=("Economics",),
            commit_script="document.querySelector('#options').remove(); input.value = ''; input.setAttribute('aria-expanded', 'false');",
        )
    )
    field = _field("Discipline", "education--0", control_type="combobox")

    with pytest.raises(RuntimeError) as error:
        GreenhouseAdapter().fill(browser_page, field, "Economics")

    assert error.value.reason_code == "commit_failed"
    assert browser_page.locator("#selected").inner_text() == ""


def test_genuine_combobox_write_failure_is_blank_and_typed(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        _combobox_form(
            options=(),
            commit_script="",
        )
    )
    browser_page.locator("#field").evaluate("element => { element.readOnly = true; }")
    field = _field("School", "school--0", control_type="combobox")

    with pytest.raises(RuntimeError) as error:
        GreenhouseAdapter().fill(browser_page, field, "Northbridge College")

    assert error.value.reason_code == "commit_failed"
    assert browser_page.locator("#selected").inner_text() == ""
    assert browser_page.locator("#field").input_value() == ""


def test_education_and_github_values_require_stored_profile_evidence() -> None:
    classifier = DeterministicClassifier()
    assert classifier.classify(
        FormQuestion("What is your GitHub username?", "text", name="github")
    ).canonical_key is CanonicalKey.GITHUB

    field = _field("What is your GitHub username?", "github")
    plan = build_fill_plan(
        [field],
        classifier,
        {},
        answer_lookup=lambda _key, _label: "invented-handle",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    assert plan.actions[0].value is None
    assert plan.actions[0].source == "missing"
    assert "invented-handle" not in repr(plan)


def test_gpa_question_is_not_misclassified_as_degree() -> None:
    field = _field(
        "For your most recent degree, what is/was your GPA (normalized to a 4.0 scale)?",
        "gpa--0",
    )
    mapping = DeterministicClassifier().classify(field.question)
    assert mapping.canonical_key is CanonicalKey.UNKNOWN

    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {CanonicalKey.DEGREE.value: "Bachelor of Science, Finance"},
        answer_lookup=lambda _key, _label: "4.0",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )
    assert plan.actions[0].value is None
    assert plan.actions[0].source == "unmapped"


@pytest.mark.parametrize(
    "label",
    [
        # "How did you hear about this job?" was removed from this list because
        # answer.source is an approved stored answer whose prompt is
        # "How did you hear about us?". The classifier now maps it to
        # CanonicalKey.SOURCE, which consults the user's approved answer
        # rather than fabricating one.
        "Employment eligibility status",
        "Have you participated in any of the following mathematics competitions?",
        "Have you completed any internships? If yes, were any at a hedge fund or proprietary trading firm?",
        "What are your annualized total compensation expectations?",
    ],
)
def test_protected_and_factual_questions_never_use_answer_lookup(label: str) -> None:
    field = _field(label, "question--0")
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {},
        answer_lookup=lambda _key, _label: "unsafe-answer",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    assert plan.actions[0].value is None
    assert "unsafe-answer" not in repr(plan)


def test_graduation_tier_conflict_blank_reason_names_stored_and_implied_years() -> None:
    field = _field(
        "What year are you expected to graduate?",
        "graduation_year",
        field_type="select",
        options=("2028", "2029"),
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {
            CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value: True,
            "guard.programme_graduation_stored": 2029,
            "guard.programme_graduation_tier": 2028,
            CanonicalKey.GRADUATION_YEAR.value: 2029,
        },
        answer_lookup=lambda _key, _label: "2029",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.value is None
    assert action.source == "graduation_tier_guard"
    finding = next(
        finding for finding in plan.risk.findings
        if finding.code == "programme_tier_graduation_conflict"
    )
    assert "2029" in finding.reason and "2028" in finding.reason


def test_needs_you_renders_each_blank_reason_and_human_submit_boundary(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    app.state.ui_v2_enabled = True
    with TestClient(app) as client:
        with client.app.state.db.session_scope() as session:
            opportunity = Opportunity(
                employer="Evidence Capital",
                role_title="Quantitative Summer Analyst",
                programme_group="summer",
                cycle="2027",
                location="London",
                url="https://example.test/evidence-source",
                application_url="https://example.test/evidence-application",
                target_status="HUMAN_CHALLENGE",
                source="phase31-test",
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.NEEDS_USER.value,
                priority=90,
                risk_level=3,
                next_action="CAPTCHA needs solving",
            )
            session.add(application)
            session.flush()
            run = AutomationRun(
                application_id=application.id,
                mode="PREFILL",
                state="NEEDS_USER",
                adapter="greenhouse",
                risk_level=3,
            )
            session.add(run)
            session.flush()
            session.add_all(
                [
                    QuestionRecord(
                        run_id=run.id,
                        label="School",
                        field_type="combobox",
                        canonical_key=CanonicalKey.UNIVERSITY.value,
                        answer_source="no_matching_option",
                        mapping_status="blocked",
                        reason="No offered institution matched",
                    ),
                    QuestionRecord(
                        run_id=run.id,
                        label="Employment eligibility status",
                        field_type="select",
                        canonical_key=CanonicalKey.WORK_AUTHORISATION.value,
                        answer_source="human",
                        mapping_status="blocked",
                        reason="Legal declaration requires the candidate",
                    ),
                ]
            )

        response = client.get("/needs-you?group=captcha")

    assert response.status_code == 200
    assert "Evidence Capital" in response.text
    assert "Quantitative Summer Analyst" in response.text
    assert "School" in response.text
    assert "No offered institution matched" in response.text
    assert "Employment eligibility status" in response.text
    assert "Legal declaration requires the candidate" in response.text
    assert "Solve the CAPTCHA" in response.text
    assert "Press Submit yourself" in response.text
