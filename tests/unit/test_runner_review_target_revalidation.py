from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from app.automation import runner as runner_module
from app.automation.adapters.registry import AdapterRegistry
from app.automation.classifier import DeterministicClassifier
from app.automation.runner import AutomationRunner, SubmissionBlocked, _OwnerThreadJourney
from app.automation.targets import TargetResolution
from app.automation.types import InspectedField, RunMode, SessionState
from app.domain.questions import FormQuestion
from app.domain.targets import TargetKind
from app.models import Application, Opportunity


def test_greenhouse_bootstrap_urls_do_not_trigger_script_exfiltration_guard() -> None:
    """Page-authored configuration URLs are not scripted data exfiltration."""

    url = "https://job-boards.greenhouse.io/point72/jobs/8423978002"
    opportunity = Opportunity(
        employer="Point72",
        role_title="Analyst Internship",
        programme_group="summer",
        cycle="2027",
        url=url,
    )
    resolution = TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:point72"},
    )
    journey = _OwnerThreadJourney(
        SimpleNamespace(
            registry=AdapterRegistry(),
            classifier=DeterministicClassifier(),
        ),
        application_id="point72-application",
        opportunity=opportunity,
        resolution=resolution,
        mode=RunMode.REVIEW,
        profile_values={"identity.first_name": "Alex"},
        answers={},
        documents={},
    )
    markup = f"""
    <!doctype html><html><body>
      <main data-employer="Point72" data-role="Analyst Internship">
        <form id="grnhse_app" class="greenhouse-form" data-ats-application
              data-role="Analyst Internship" action="{url}" method="post">
          <label>First name<input name="first_name" required></label>
          <button type="submit">Submit application</button>
        </form>
      </main>
      <script>
        window.__remixContext = {{
          assets: "https://job-boards.cdn.greenhouse.io/assets/entry.js"
        }};
      </script>
    </body></html>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "https://job-boards.greenhouse.io/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=markup,
            ),
        )
        page.goto(url)
        outcome = journey._run_steps(page)
        browser.close()

    assert "submission_network_guard" not in outcome["blocked_reasons"]


def test_prefill_populates_approved_fields_before_required_answer_handoff() -> None:
    class _Adapter:
        name = "greenhouse"

        def __init__(self) -> None:
            self.filled: list[tuple[str, str]] = []

        @staticmethod
        def detect_human_boundary(_page):
            return False, ""

        @staticmethod
        def inspect_with_evidence(_page):
            return [
                InspectedField(
                    selector="#first-name",
                    question=FormQuestion(
                        label="First name",
                        name="first_name",
                        field_type="text",
                        required=True,
                    ),
                    control_type="text",
                ),
                InspectedField(
                    selector="#employer-question",
                    question=FormQuestion(
                        label="Employer-specific required question",
                        name="custom_required",
                        field_type="text",
                        required=True,
                    ),
                    control_type="text",
                ),
            ], {
                "root_found": True,
                "page_root_found": True,
                "root_selector": "#application-form",
                "root_token": "root-token",
                "submit_present": True,
            }

        def fill(self, _scope, field, value) -> None:
            self.filled.append((field.question.name, value))

        @staticmethod
        def verify_step(_scope, _fields, _values) -> bool:
            return True

    class _Registry:
        def __init__(self, adapter) -> None:
            self.adapter = adapter

        def detect(self, _resolution):
            return self.adapter

    adapter = _Adapter()
    runner = SimpleNamespace(
        registry=_Registry(adapter),
        classifier=DeterministicClassifier(),
    )
    url = "http://127.0.0.1:8787/lab/ats/prefill"
    journey = _OwnerThreadJourney(
        runner,
        application_id="partial-prefill",
        opportunity=Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group="summer",
            cycle="2027",
            url=url,
        ),
        resolution=TargetResolution(
            source_url=url,
            final_url=url,
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            evidence={"synthetic_lab": True},
        ),
        mode=RunMode.PREFILL,
        profile_values={"identity.first_name": "Alex"},
        answers={},
        documents={},
    )
    page = SimpleNamespace(url=url, evaluate=lambda _script: {})

    outcome = journey._run_steps(page)

    assert outcome["state"] == "NEEDS_USER"
    assert "unknown_required_field" in outcome["blocked_reasons"]
    assert adapter.filled == [("first_name", "Alex")]
    assert outcome["manifest"]["prefilled_fields"] == ["first_name"]
    assert outcome["manifest"]["submission"] == "not_clicked"


def _verified_resolution(url: str) -> TargetResolution:
    return TargetResolution(
        source_url="http://127.0.0.1:8787/source",
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": "greenhouse",
            "application_origin": "http://127.0.0.1:8787",
            "employer": "Example Employer",
            "role": "Analyst",
            "requisition": "/application",
            "form_identity": "application",
        },
    )


class _Session:
    def __init__(self, application: Application, opportunity: Opportunity) -> None:
        self.application = application
        self.opportunity = opportunity
        self.expired = False

    def get(self, model, identifier):
        if model is Application and identifier == self.application.id:
            return self.application
        if model is Opportunity and identifier == self.opportunity.id:
            return self.opportunity
        return None

    def add(self, _value) -> None:
        return None

    def flush(self) -> None:
        return None

    def refresh(self, _value) -> None:
        return None

    def expire_all(self) -> None:
        self.expired = True


class _Navigator:
    def __init__(self, opportunity: Opportunity) -> None:
        self.opportunity = opportunity
        self.journey = None

    def start(self, *_args, **_kwargs):
        return SimpleNamespace(session_id="review-session")

    def run_journey(self, _session_id: str) -> None:
        # This is the race: target resolution succeeded before the browser
        # journey, then the opportunity is invalidated before FINAL_REVIEW is
        # converted into the durable READY_TO_SUBMIT state.
        self.opportunity.application_url = None
        self.opportunity.target_status = TargetKind.UNRESOLVED.value
        self.opportunity.resolved_at = None

    def wait_for_journey_result(self, _session_id: str, *, timeout: int):
        return {
            "state": "FINAL_REVIEW",
            "risk_level": 0,
            "blocked_reasons": (),
            "adapter": "greenhouse",
        }

    def get(self, _session_id: str):
        return SimpleNamespace(
            state=SessionState.ACTIVE,
            human_boundary={},
        )

    def close(self, _session_id: str, *, reason: str) -> None:
        return None


class _Run:
    def __init__(self, **_kwargs) -> None:
        self.id = "run-1"
        self.trace_path = ""
        self.screenshot_path = ""
        self.state = ""
        self.error = ""
        self.finished_at = None

    def mark_risk_assessed(self, *_args, **_kwargs) -> None:
        return None


class _Adapter:
    name = "greenhouse"


class _Registry:
    def detect(self, _resolution):
        return _Adapter()


@pytest.mark.parametrize("mode", [RunMode.REVIEW, RunMode.PREFILL])
def test_review_revalidates_target_before_promoting_final_review(
    monkeypatch, tmp_path: Path, mode: RunMode
) -> None:
    opportunity = Opportunity(
        id="opp-review-race",
        employer="Example Employer",
        role_title="Analyst",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source",
        application_url="http://127.0.0.1:8787/application",
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=SimpleNamespace(),
    )
    application = Application(
        id="application-review-race",
        opportunity_id=opportunity.id,
        state="FILLING",
    )
    session = _Session(application, opportunity)
    navigator = _Navigator(opportunity)
    runner = object.__new__(AutomationRunner)
    runner.settings = SimpleNamespace(
        traces_dir=tmp_path / "traces",
        screenshots_dir=tmp_path / "screenshots",
    )
    runner.registry = _Registry()
    runner._handoff_manager = navigator
    runner._active_navigator_lease = None

    resolutions = iter(
        [
            _verified_resolution("http://127.0.0.1:8787/application"),
            SubmissionBlocked(
                "No verified application URL is available",
                code="target_unresolved",
            ),
        ]
    )

    def resolve(_opportunity: Opportunity):
        result = next(resolutions)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runner, "_resolution_from_opportunity", resolve)
    monkeypatch.setattr(
        runner,
        "_approved_inputs",
        lambda _session, _application, _framing: ({}, {}, {}, {}, []),
    )
    monkeypatch.setattr(
        runner,
        "_navigator_for_run",
        lambda _headed: (navigator, False),
    )
    monkeypatch.setattr(runner_module, "AutomationRun", _Run)
    monkeypatch.setattr(runner_module, "append_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner_module.AutomationRunner,
        "_settled_handoff_snapshot",
        staticmethod(lambda *_args, **_kwargs: None),
    )

    outcome = runner._run_claimed_owner(
        session,
        application,
        application.id,
        mode,
        headed=True,
    )

    assert outcome.state == "NEEDS_USER"
    assert application.state == "NEEDS_USER"
    assert application.state != "READY_TO_SUBMIT"
    assert "target" in " ".join(outcome.blocked_reasons).casefold()
    assert session.expired is True
