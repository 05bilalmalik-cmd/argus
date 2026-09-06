# TDD: R2 — multi-step application state machine.
# The runner must drive landing -> apply -> apply-manually -> multiple
# dynamic steps, inspecting/filling/validating EACH step before advancing,
# and never confuse Next/Save with final submit.
from __future__ import annotations

from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright


STEP1 = """
<div id="step-1" class="wd-step">
  <label for="wd-first">First name</label><input id="wd-first" name="first_name">
  <label for="wd-last">Last name</label><input id="wd-last" name="last_name">
  <button type="button" data-automation-id="submitNextButton">Next</button>
</div>
"""

FULL_JOURNEY = """
<html><body>
<main data-ats="workday" data-employer="MultiStep Corp" data-role="Summer Analyst">
  <div id="landing">
    <h1>Summer Analyst</h1>
    <button type="button" data-automation-id="adventureButton">Apply</button>
  </div>
</main>
<script>
  const state = { step: 0 };
  window.__wdState = state;
  document.querySelector('[data-automation-id="adventureButton"]').onclick = () => {
    document.getElementById('landing').remove();
    const dialog = document.createElement('div');
    dialog.id = 'signin';
    dialog.innerHTML = '<button type="button" data-automation-id="applyManually">Apply Manually</button>';
    document.body.append(dialog);
    dialog.querySelector('[data-automation-id="applyManually"]').onclick = () => {
      dialog.remove();
      renderStep(0);
    };
  };
  function renderStep(n) {
    state.step = n;
    const existing = document.querySelectorAll('.wd-step');
    existing.forEach(e => e.remove());
    const host = document.createElement('div');
    host.className = 'wd-step';
    if (n === 0) {
      host.id = 'step-contact';
      host.innerHTML = `
        <form class="ats-form wd-form" onsubmit="return false">
          <label for="f">First name</label><input id="f" name="first_name">
          <label for="l">Last name</label><input id="l" name="last_name">
          <p id="err" style="display:none;color:red">Required fields missing</p>
          <button type="button" data-automation-id="submitNextButton" onclick="advance()">Next</button>
        </form>`;
    } else {
      host.id = 'step-final';
      host.innerHTML = `
        <form class="ats-form wd-form" method="post" action="/lab/ats/workday-journey/submit" onsubmit="window.__finalPosted=(window.__finalPosted||0)+1; return true;">
          <p>Review your application.</p>
          <button type="submit" data-automation-id="submitButton">Submit Application</button>
        </form>`;
    }
    document.body.append(host);
  }
  window.advance = () => {
    const f = document.getElementById('f'), l = document.getElementById('l');
    if (!f.value || !l.value) {
      const err = document.getElementById('err');
      err.style.display = 'block';
      return; // validation error: stay on this step
    }
    renderStep(1);
  };
</script>
</body></html>
"""


class TestWorkdayStateMachine:
    def test_step_machine_advances_only_after_validation(self):
        from app.automation.adapters.workday import WorkdayAdapter

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(FULL_JOURNEY)
            adapter = WorkdayAdapter()

            # Landing: enter_application_flow drives Apply + Apply Manually.
            assert adapter.enter_application_flow(page) is True
            assert page.query_selector("#step-contact") is not None

            # Step inspection sees the contact fields.
            fields, evidence = adapter.inspect_with_evidence(page)
            names = {f.question.name for f in fields}
            assert {"first_name", "last_name"} <= names
            # No submit button on step 0: only Next.
            assert not adapter.is_final_submit_visible(page)

            # Advancing without filling must NOT advance (validation error).
            advanced = adapter.advance_if_valid(page)
            assert advanced is False
            assert page.query_selector("#step-contact") is not None

            browser.close()

    def test_final_step_exposes_exactly_one_real_submit(self):
        from app.automation.adapters.workday import WorkdayAdapter

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(FULL_JOURNEY)
            adapter = WorkdayAdapter()
            adapter.enter_application_flow(page)
            page.evaluate("() => window.renderStep(1)")

            assert adapter.is_final_submit_visible(page)
            browser.close()

    def test_next_button_is_never_a_submission_candidate(self):
        from app.automation.adapters.workday import WorkdayAdapter

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(
                '<div class="wd-step"><form>'
                '<input name="a"><button type="button" '
                'data-automation-id="submitNextButton">Next</button>'
                "</form></div>"
            )
            adapter = WorkdayAdapter()
            assert not adapter.is_final_submit_visible(page)
            browser.close()

    def test_sign_in_email_is_not_application_form_proof(self):
        from app.automation.adapters.workday import WorkdayAdapter

        html = """
        <main data-ats="workday" data-employer="Proof Corp" data-role="Analyst">
          <h1>Analyst</h1>
          <button type="button" data-automation-id="adventureButton">Apply</button>
        </main>
        <script>
          document.querySelector('[data-automation-id="adventureButton"]').onclick = () => {
            document.querySelector('main').innerHTML =
              '<label>Email<input data-automation-id="email" type="email"></label>';
          };
        </script>
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            adapter = WorkdayAdapter()

            # Destination identity is established before this click by the
            # runner; an email-only sign-in surface is not an application.
            assert adapter.verify_destination_identity(
                page, expected_employer="Proof Corp", expected_role="Analyst"
            ) is True
            page.locator('[data-automation-id="adventureButton"]').click()
            assert adapter.enter_application_flow(page) is False
            fields, evidence = adapter.inspect_with_evidence(page)
            assert fields == []
            assert evidence["root_found"] is False
            browser.close()

    def test_step_marker_proves_dynamic_same_id_step_changed(self):
        from app.automation.adapters.workday import WorkdayAdapter

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(
                '<div class="wd-step" id="same-step"><input name="first_name"></div>'
            )
            adapter = WorkdayAdapter()
            before = adapter.current_step_marker(page)
            page.evaluate(
                """() => { document.querySelector('.wd-step').innerHTML =
                '<input name="last_name"><select name="graduation_year"><option>2029</option></select>'; }"""
            )
            after = adapter.current_step_marker(page)
            assert before != after
            browser.close()

    def test_verify_step_requires_actual_values_validity_and_no_visible_errors(self):
        from app.automation.adapters.workday import WorkdayAdapter

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(
                '<div class="wd-step"><form>'
                '<label>First name<input name="first_name" required></label>'
                '<p class="error" style="display:none">Required</p>'
                '</form></div>'
            )
            adapter = WorkdayAdapter()
            fields, evidence = adapter.inspect_with_evidence(page)
            assert fields
            assert adapter.verify_step(page, fields, {"first_name": "Demo"}) is False
            page.locator('input[name="first_name"]').fill("Demo")
            assert adapter.verify_step(page, fields, {"first_name": "Demo"}) is True
            browser.close()


def test_runner_replans_when_same_step_inventory_changes_before_next():
    """A newly-rendered field must be mapped/verified before any Next click."""

    from app.automation.runner import _OwnerThreadJourney
    from app.automation.targets import TargetResolution
    from app.automation.types import RunMode
    from app.domain.targets import TargetKind

    first = SimpleNamespace(
        selector="#first",
        question=SimpleNamespace(name="first_name", label="First name"),
        control_type="text",
    )
    newly_rendered = SimpleNamespace(
        selector="#graduation",
        question=SimpleNamespace(name="graduation_year", label="Graduation year"),
        control_type="text",
    )
    inspections = [
        [first],
        [first, newly_rendered],
        [first, newly_rendered],
        [first, newly_rendered],
    ]
    build_calls: list[tuple[str, ...]] = []
    filled: list[str] = []

    class Adapter:
        name = "workday"

        def fill(self, _scope, field, value):
            filled.append(field.question.name)

        def advance_one_step(self, _scope):
            # Reaching Next with only one planning pass would be a safety bug.
            assert len(build_calls) >= 2
            return False

        def is_final_submit_visible(self, _scope):
            return False

    journey = object.__new__(_OwnerThreadJourney)
    journey.application_id = "dynamic-step"
    journey.opportunity = SimpleNamespace(employer="Example", role_title="Analyst")
    journey.resolution = TargetResolution(
        source_url="http://127.0.0.1:8787/source",
        final_url="http://127.0.0.1:8787/application",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"synthetic_lab": True},
    )
    journey.mode = RunMode.PREFILL
    journey.adapter = Adapter()
    journey.adapter_name = "workday"
    journey.step_index = 0
    journey.last_scope = None
    journey.last_fields = []
    journey.last_evidence = {}
    journey.last_plan = None
    journey.final_handle = None
    journey.final_target = None
    journey.final_scope = None
    journey.target_error = ""
    journey._human_stage = ""
    journey._boundary = lambda _page: (False, "")
    journey._inspect = lambda _page: (
        inspections.pop(0),
        {"root_found": True, "step_name": "same-step", "root_selector": "#step"},
        object(),
    )
    journey._build_plan = lambda fields, _evidence: (
        build_calls.append(tuple(field.question.name for field in fields))
        or SimpleNamespace(
            actions=[
                SimpleNamespace(
                    field=field,
                    value=field.question.name,
                    status="resolved",
                )
                for field in fields
            ],
            risk=SimpleNamespace(level=0, blocking_codes=()),
        )
    )
    journey._verify_after_fill = lambda _scope, _fields, _values: True

    result = journey._run_steps(SimpleNamespace(evaluate=lambda _script: {}))

    assert result["state"] == "NEEDS_USER"
    assert build_calls == [("first_name",), ("first_name", "graduation_year")]
    assert filled == ["first_name", "first_name", "graduation_year"]
