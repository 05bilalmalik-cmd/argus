from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Page, sync_playwright

from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.domain.states import UserApplicationStatus
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, AuditOutbox, Opportunity
from app.services.navigator import _SourceResolutionExecutor
from app.services.resolution_apply_click import (
    ApplyCandidate,
    ApplyAffordance,
    ApplyClickBudget,
    ApplyClickResult,
    ApplyClickSafetyViolation,
    guarded_apply_click,
    is_explicit_apply_name,
    is_submission_or_confirmation_landing,
    select_ranked_apply_candidate,
)
from app.services.target_resolution import TargetResolutionService


def _database(tmp_path: Path) -> Database:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return database


@pytest.fixture(scope="module")
def _browser_page() -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def test_opaque_requisition_apply_probe_requires_role_and_employer_root(
    _browser_page: Page,
) -> None:
    _browser_page.set_content(
        """
        <main>
          <article id="job">
            <h1>Placement, Client Service, 2027</h1>
            <p>AlphaSights</p>
            <button type="button">Apply Now</button>
          </article>
        </main>
        """
    )
    executor = _SourceResolutionExecutor(
        source_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        inspection_url="https://www.alphasights.com/job/placement-client-service-2027/",
        authoritative_requisition="",
        provider_hint="unknown",
        employer="AlphaSights",
        role_title="Placement, Client Service, 2027",
        apply_click_enabled=True,
    )

    assert executor._discover_application_link(_browser_page) == ""
    affordance = executor._discover_apply_affordance(
        _browser_page,
        apply_click_identity_only=True,
    )

    assert affordance is not None
    assert affordance.accessible_name == "Apply Now"


def test_identity_root_mutation_refuses_before_click(_browser_page: Page) -> None:
    _browser_page.set_content(
        """
        <main>
          <article>
            <h1>Placement, Client Service, 2027</h1>
            <p>AlphaSights</p>
            <button id="apply" type="button">Apply Now</button>
          </article>
        </main>
        <script>
          document.querySelector('#apply').addEventListener('click', () => {
            window.__argusClicked = true;
          });
        </script>
        """
    )
    executor = _SourceResolutionExecutor(
        source_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        inspection_url="https://www.alphasights.com/job/placement-client-service-2027/",
        authoritative_requisition="",
        provider_hint="unknown",
        employer="AlphaSights",
        role_title="Placement, Client Service, 2027",
        apply_click_enabled=True,
    )
    affordance = executor._discover_apply_affordance(
        _browser_page,
        apply_click_identity_only=True,
    )

    assert affordance is not None
    _browser_page.locator("h1").evaluate(
        "element => element.textContent = 'Different role'"
    )
    result = guarded_apply_click(_browser_page, affordance, timeout_ms=1000)

    assert result.clicked is False
    assert result.outcome == "apply_click_identity_root_changed"
    assert _browser_page.evaluate("() => Boolean(window.__argusClicked)") is False


def test_plain_js_apply_button_is_explicitly_discoverable_without_url() -> None:
    # This is the contract consumed by the browser-side discovery script: a
    # button needs an explicit apply name, not an href or predicted URL.
    assert is_explicit_apply_name("Apply") is True
    assert is_explicit_apply_name("Apply Now") is True
    assert is_explicit_apply_name("Start Application") is True


@pytest.mark.parametrize(
    "name",
    (
        "",
        "Continue",
        "Next",
        "Submit application",
        "Send",
        "Confirm",
        "Delete",
        "Withdraw",
        "Sign out",
        "Autofill my application",
        "Pay application fee",
    ),
)
def test_unknown_and_dangerous_names_are_not_apply_controls(name: str) -> None:
    assert is_explicit_apply_name(name) is False


def test_first_party_apply_outranks_social_control() -> None:
    selected = select_ranked_apply_candidate(
        (
            ApplyCandidate(
                accessible_name="Apply with LinkedIn",
                role="button",
                href="https://www.linkedin.com/jobs/view/example",
                inside_job_root=True,
                first_party=False,
                third_party=True,
            ),
            ApplyCandidate(
                accessible_name="Apply",
                role="button",
                href="https://employer.example/jobs/example/apply",
                inside_job_root=True,
                first_party=True,
            ),
        )
    )

    assert selected is not None
    assert selected.accessible_name == "Apply"


def test_indistinguishable_first_party_controls_refuse() -> None:
    assert (
        select_ranked_apply_candidate(
            (
                ApplyCandidate(
                    accessible_name="Apply",
                    role="button",
                    href="https://employer.example/jobs/example/apply-a",
                    inside_job_root=True,
                    first_party=True,
                ),
                ApplyCandidate(
                    accessible_name="Apply",
                    role="button",
                    href="https://employer.example/jobs/example/apply-b",
                    inside_job_root=True,
                    first_party=True,
                ),
            )
        )
        is None
    )


def test_apply_click_accepts_an_observed_source_redirect_origin_only() -> None:
    from app.services.resolution_apply_click import ApplyClickBudget

    source_url = "https://source.example/opportunity"
    redirected_url = "https://employer.example/jobs/analyst"
    capability = SimpleNamespace(
        capability_id="phase28-redirect-capability",
        source_url=source_url,
        hostname="source.example",
        origin="https://source.example",
        active=True,
    )
    executor = _SourceResolutionExecutor(
        source_url=source_url,
        inspection_url=source_url,
        provider_hint="unknown",
        employer="Example Employer",
        role_title="Analyst",
        allowlist=frozenset({"source.example"}),
        apply_click_enabled=True,
        apply_click_budget=ApplyClickBudget(1),
        source_capability=capability,
    )
    executor._apply_click_active = True
    executor.record_redirect_chain((source_url, redirected_url))

    assert executor.authorizes_apply_document_request(redirected_url, ()) is True
    assert (
        executor.authorizes_apply_document_request(
            "https://untrusted.example/application", ()
        )
        is False
    )


def test_apply_click_preflight_accepts_observed_redirect_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = "https://grnh.se/opaque-job"
    redirected_url = "https://employer.example/jobs/analyst"
    capability = SimpleNamespace(
        capability_id="phase28-redirect-capability",
        source_url=source_url,
        hostname="grnh.se",
        origin="https://grnh.se",
        active=True,
    )
    executor = _SourceResolutionExecutor(
        source_url=source_url,
        inspection_url=source_url,
        provider_hint="unknown",
        employer="Example Employer",
        role_title="Analyst",
        allowlist=frozenset({"grnh.se"}),
        apply_click_enabled=True,
        apply_click_budget=ApplyClickBudget(1),
        source_capability=capability,
    )
    executor.record_redirect_chain((source_url, redirected_url))
    executor._discover_apply_affordance = lambda _page, **_kwargs: ApplyAffordance(
        "[data-argus-apply]", "Apply", "button"
    )
    monkeypatch.setattr(
        "app.services.navigator.guarded_apply_click",
        lambda _page, _affordance, *, timeout_ms: ApplyClickResult(
            False, "test_refused", "Apply", "button"
        ),
    )

    outcome = executor._attempt_guarded_apply_click(
        SimpleNamespace(url=redirected_url)
    )

    assert outcome is False
    assert executor._apply_click_evidence["outcome"] == "test_refused"


def test_client_side_source_navigation_extends_capability_only_for_allowlisted_chain() -> None:
    source_url = "https://grnh.se/opaque-job"
    intermediate_url = "https://www.alphasights.com/careers/open-roles"
    final_url = "https://www.alphasights.com/job/placement-client-service-2027/"
    capability = SimpleNamespace(
        capability_id="phase28-client-navigation-capability",
        source_url=source_url,
        hostname="grnh.se",
        origin="https://grnh.se",
        active=True,
    )
    executor = _SourceResolutionExecutor(
        source_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        inspection_url=source_url,
        provider_hint="unknown",
        employer="AlphaSights",
        role_title="Placement, Client Service, 2027",
        allowlist=frozenset({"grnh.se", "www.alphasights.com"}),
        apply_click_enabled=True,
        apply_click_budget=ApplyClickBudget(1),
        source_capability=capability,
    )

    executor.record_source_navigation(source_url)
    executor.record_source_navigation(intermediate_url)
    executor.record_source_navigation(final_url)

    executor._apply_click_active = True
    allowed = executor._apply_click_allowed_origins()

    assert "https://grnh.se" in allowed
    assert "https://www.alphasights.com" in allowed
    assert "https://untrusted.example" not in allowed


def test_confirmation_or_submitted_landing_is_fatal() -> None:
    assert (
        is_submission_or_confirmation_landing(
            url="https://employer.example/application/confirmation",
            title="Application received",
            text="Thank you for applying. Your application has been submitted.",
        )
        is True
    )
    with pytest.raises(ApplyClickSafetyViolation):
        ApplyClickSafetyViolation.raise_if_landing_is_submission(
            url="https://employer.example/application/confirmation",
            title="Application received",
            text="Thank you for applying.",
        )


class _DestinationPage:
    url = "https://job-boards.greenhouse.io/example/jobs/123/application"

    def content(self) -> str:
        return (
            "<html><body><form><input name='email'>"
            "<button type='submit'>Submit application</button></form></body></html>"
        )

    def evaluate(self, _script: str, *_args: object) -> dict[str, object]:
        return {
            "provider": "greenhouse",
            "employer": "Wrong Employer",
            "role": "Wrong Role",
            "requisition": "123",
            "form_identity": "123",
            "root_selector": "[data-argus-form-root='one']",
            "form_action": self.url,
            "form_visible": True,
            "application_entry_visible": True,
            "control_count": 2,
            "submit_present": True,
        }


def _click_executor() -> _SourceResolutionExecutor:
    executor = _SourceResolutionExecutor(
        source_url="https://source.example/opportunity",
        inspection_url="https://job-boards.greenhouse.io/example/jobs/123",
        authoritative_requisition="123",
        requisition_source="stored_inspection_url",
        provider_hint="greenhouse",
        employer="Example Employer",
        role_title="Analyst",
        apply_click_enabled=True,
    )
    executor._apply_origin_resolution = TargetResolution(
        source_url=executor.source_url,
        final_url=executor.inspection_url,
        kind=TargetKind.JOB_DETAIL,
        provider="greenhouse",
        reason_codes=("direct_ats_job",),
    )
    executor._apply_click_evidence = {
        "attempted": True,
        "clicked": True,
        "accessible_name": "Apply",
        "page_url": executor.inspection_url,
        "result_url": _DestinationPage.url,
    }
    return executor


def test_post_click_identity_mismatch_is_not_promoted_and_marks_discard() -> None:
    executor = _click_executor()

    executor._inspect(_DestinationPage())

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert executor._apply_click_evidence["outcome"] == (
        "apply_click_destination_mismatch"
    )
    assert executor._apply_click_evidence["context_discarded"] is True


def test_apply_click_mismatch_leaves_opportunity_row_exactly_unchanged(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    source_url = "https://source.example/opportunity"
    previous_resolved_at = datetime.now(timezone.utc) - timedelta(days=1)
    previous_attempted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    previous_evidence = {
        "stable": "keep",
        "verified_target": {
            "application_url": "https://job-boards.greenhouse.io/example/jobs/123/apply",
            "resolved_ats_type": "greenhouse",
            "resolved_at": previous_resolved_at.isoformat(),
            "target_status": "APPLICATION_FORM",
        },
    }
    try:
        with database.session_scope() as session:
            opportunity = Opportunity(
                employer="Example Employer",
                role_title="Analyst",
                programme_group="spring_week",
                cycle="2027",
                url=source_url,
                application_url=(
                    "https://job-boards.greenhouse.io/example/jobs/123/apply"
                ),
                target_status=TargetKind.JOB_DETAIL.value,
                resolved_ats_type="greenhouse",
                resolved_at=previous_resolved_at,
                resolution_attempted_at=previous_attempted_at,
                resolution_evidence_json=json.dumps(previous_evidence),
                application_window_status="OPEN",
            )
            session.add(opportunity)
            session.flush()
            opportunity_id = opportunity.id
            before = {
                "target_status": opportunity.target_status,
                "application_url": opportunity.application_url,
                "resolved_ats_type": opportunity.resolved_ats_type,
                "resolved_at": opportunity.resolved_at,
                "resolution_attempted_at": opportunity.resolution_attempted_at,
                "resolution_evidence_json": opportunity.resolution_evidence_json,
                "updated_at": opportunity.updated_at,
            }

            mismatch = TargetResolution(
                source_url=source_url,
                final_url=source_url,
                kind=TargetKind.JOB_DETAIL,
                provider="greenhouse",
                reason_codes=("apply_click_destination_mismatch",),
                evidence={
                    "apply_click": {
                        "attempted": True,
                        "clicked": True,
                        "accessible_name": "Apply",
                        "page_url": source_url,
                        "result_url": "https://job-boards.greenhouse.io/example/jobs/999",
                        "outcome": "apply_click_destination_mismatch",
                        "destination_verification": {
                            "verdict": "mismatch",
                            "employer_bound": False,
                            "role_bound": False,
                        },
                    }
                },
            )
            outcome = TargetResolutionService(session).resolve(
                opportunity_id,
                resolver=lambda _context: mismatch,
            )

            assert outcome.target_status == TargetKind.JOB_DETAIL.value
            assert {
                "target_status": opportunity.target_status,
                "application_url": opportunity.application_url,
                "resolved_ats_type": opportunity.resolved_ats_type,
                "resolved_at": opportunity.resolved_at,
                "resolution_attempted_at": opportunity.resolution_attempted_at,
                "resolution_evidence_json": opportunity.resolution_evidence_json,
                "updated_at": opportunity.updated_at,
            } == before

            audit_rows = list(
                session.execute(
                    __import__("sqlalchemy").select(
                        __import__("app.models", fromlist=["AuditOutbox"]).AuditOutbox.event_type
                    )
                ).scalars()
            )
            assert "opportunity.apply_click_resolution" in audit_rows
    finally:
        database.engine.dispose()


def test_no_state_is_reused_after_failed_probe() -> None:
    # The navigator must expose the disposable-context result explicitly so a
    # caller cannot mistake a failed click probe for a resumable session.
    executor = _click_executor()
    executor._apply_click_evidence["context_discarded"] = True
    assert executor._apply_click_evidence["context_discarded"] is True


def test_fatal_apply_click_is_audited_and_halts_without_mutating_rows(
    tmp_path: Path,
) -> None:
    from app.services.batch_target_resolution import (
        BatchResolveOptions,
        BatchTargetResolutionDriver,
    )

    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_APPLY_CLICK": "true",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    first_id = "phase28-fatal-first"
    second_id = "phase28-fatal-second"
    try:
        with database.session_scope() as session:
            for opportunity_id in (first_id, second_id):
                opportunity = Opportunity(
                    id=opportunity_id,
                    employer="Example Employer",
                    role_title="Analyst",
                    programme_group="spring_week",
                    cycle="2027",
                    url=f"https://jobs.example.test/{opportunity_id}",
                    target_status=TargetKind.UNRESOLVED.value,
                    user_status=UserApplicationStatus.NOT_APPLIED.value,
                    application_window_status="OPEN",
                    resolution_evidence_json='{"stable":"keep"}',
                )
                session.add(opportunity)
                session.flush()
                session.add(
                    Application(
                        opportunity_id=opportunity_id,
                        state=ApplicationState.DISCOVERED.value,
                    )
                )

        calls: list[str] = []

        def resolver(context) -> TargetResolution:  # noqa: ANN001
            calls.append(context.opportunity_id)
            if context.opportunity_id == first_id:
                raise ApplyClickSafetyViolation(
                    "synthetic confirmation landing",
                    evidence={
                        "attempted": True,
                        "clicked": True,
                        "accessible_name": "Apply",
                        "pre_click_url": "https://jobs.example.test/phase28-fatal-first",
                        "post_click_url": "https://jobs.example.test/confirmation",
                        "outcome": "apply_click_submission_boundary_violation",
                        "verification_verdict": "fatal_submission_boundary",
                        "context_discarded": True,
                    },
                )
            return TargetResolution(
                source_url=context.source_url,
                final_url=context.source_url,
                kind=TargetKind.JOB_DETAIL,
                reason_codes=("synthetic_job_detail",),
            )

        before = {}
        with database.session_scope() as session:
            for opportunity_id in (first_id, second_id):
                row = session.get(Opportunity, opportunity_id)
                before[opportunity_id] = (
                    row.target_status,
                    row.application_url,
                    row.resolved_ats_type,
                    row.resolved_at,
                    row.resolution_attempted_at,
                    row.resolution_evidence_json,
                    row.updated_at,
                )

        report = BatchTargetResolutionDriver(
            database,
            settings,
            resolver_factory=lambda: resolver,
        ).run(BatchResolveOptions(concurrency=1, delay_seconds=0))

        assert report.interrupted is True
        assert report.fatal_safety_violation
        assert calls == [first_id]
        assert report.attempted == 0
        with database.session_scope() as session:
            for opportunity_id in (first_id, second_id):
                row = session.get(Opportunity, opportunity_id)
                assert (
                    row.target_status,
                    row.application_url,
                    row.resolved_ats_type,
                    row.resolved_at,
                    row.resolution_attempted_at,
                    row.resolution_evidence_json,
                    row.updated_at,
                ) == before[opportunity_id]
            events = list(
                session.scalars(
                    __import__("sqlalchemy").select(AuditOutbox).where(
                        AuditOutbox.event_type
                        == "opportunity.apply_click_resolution"
                    )
                ).all()
            )
            assert len(events) == 1
            details = json.loads(events[0].details_json)
            assert details["clicked"] is True
            assert details["pre_click_url"] == (
                "https://jobs.example.test/phase28-fatal-first"
            )
            assert details["post_click_url"] == (
                "https://jobs.example.test/confirmation"
            )
            assert details["verification_verdict"] == "fatal_submission_boundary"
    finally:
        database.engine.dispose()
