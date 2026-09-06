from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from app.config import Settings
from app.db import Database
from app.domain.targets import TargetKind
from app.automation.targets import TargetResolution
from app.models import Application, Opportunity
from app.services.navigator import _SourceResolutionExecutor
from app.services.resolution_apply_click import guarded_apply_click
from app.services.target_resolution import ResolutionContext, TargetResolutionService


@pytest.fixture(scope="module")
def browser_page() -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _database(tmp_path: Path) -> Database:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return database


@pytest.mark.parametrize(
    ("url", "expected_provider", "expected_requisition"),
    (
        (
            "https://job-boards.greenhouse.io/acme/jobs/8518528002",
            "greenhouse",
            "8518528002",
        ),
        (
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=8518528002",
            "greenhouse",
            "8518528002",
        ),
        (
            "https://travelers.wd5.myworkdayjobs.com/en-US/External/job/London/Role_R-51191",
            "workday",
            "R-51191",
        ),
        (
            "https://jobs.lever.co/acme/9f73569e-952b-4c0a-b77c-d302846f15bd",
            "lever",
            "9f73569e-952b-4c0a-b77c-d302846f15bd",
        ),
        (
            "https://apply.workable.com/acme/j/A15A62A8BE/",
            "workable",
            "A15A62A8BE",
        ),
        (
            "https://jobs.smartrecruiters.com/Wiser/744000143765020-private-funds-group",
            "smartrecruiters",
            "744000143765020",
        ),
    ),
)
def test_stored_job_url_yields_strict_requisition_identity(
    url: str,
    expected_provider: str,
    expected_requisition: str,
) -> None:
    from app.services.requisition_identity import requisition_identity_from_url

    identity = requisition_identity_from_url(url)

    assert identity is not None
    assert identity.provider == expected_provider
    assert identity.requisition == expected_requisition
    assert identity.provenance == "stored_inspection_url"


@pytest.mark.parametrize(
    "url",
    (
        "https://grnh.se/twyh9job1us",
        "https://higher.gs.com/campus?search=one+year",
        "https://www.janestreet.com/join-jane-street/programs-and-events/",
        "https://jobs.standardchartered.com/search/?q=intern+2027",
    ),
)
def test_listing_search_and_opaque_shortlink_do_not_invent_requisition(url: str) -> None:
    from app.services.requisition_identity import requisition_identity_from_url

    assert requisition_identity_from_url(url) is None


def test_requisition_matching_is_exact_not_prefix_or_suffix() -> None:
    from app.services.requisition_identity import requisitions_equal

    assert requisitions_equal("REQ-42", "req-42") is True
    assert requisitions_equal("1234", "12345") is False
    assert requisitions_equal("REQ-42", "REQ-42A") is False


def test_resolution_context_separates_trackr_provenance_and_inspection_identity(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    source = "https://app.the-trackr.com/uk-finance/spring-weeks"
    inspection = (
        "https://job-boards.greenhouse.io/embed/job_app?for=gsacapital&token=8518528002"
    )
    captured: list[ResolutionContext] = []

    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="GSA Capital",
            role_title="Women in Trading Insight Programme - 2027",
            programme_group="spring_week",
            cycle="2027",
            url=source,
            application_url=inspection,
            source="trackr",
            ats_type="greenhouse",
            application_window_status="OPEN",
        )
        application = Application(opportunity=opportunity, state="DISCOVERED")
        session.add_all((opportunity, application))
        session.flush()

        def resolver(context: ResolutionContext):  # noqa: ANN202 - capture-only resolver
            captured.append(context)
            return None

        TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=resolver,
        )

    assert len(captured) == 1
    assert captured[0].source_url == source
    assert captured[0].inspection_url == inspection
    assert captured[0].authoritative_requisition == "8518528002"
    assert captured[0].requisition_source == "stored_inspection_url"


def _requisition_executor(
    *,
    requisition: str = "8518528002",
    inspection_url: str = "https://job-boards.greenhouse.io/gsacapital/jobs/8518528002",
) -> _SourceResolutionExecutor:
    return _SourceResolutionExecutor(
        source_url="https://app.the-trackr.com/uk-finance/spring-weeks",
        inspection_url=inspection_url,
        authoritative_requisition=requisition,
        requisition_source="stored_inspection_url",
        provider_hint="greenhouse",
        employer="GSA Capital",
        role_title="Women in Trading Insight Programme - 2027",
        apply_click_enabled=True,
    )


def test_matching_requisition_binds_without_employer_or_role_prose(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main>
          <article data-requisition="8518528002">
            <img alt="company logo">
            <a href="https://job-boards.greenhouse.io/gsacapital/jobs/8518528002/apply">
              Apply
            </a>
          </article>
          <article data-requisition="8518528003">
            <a href="https://job-boards.greenhouse.io/gsacapital/jobs/8518528003/apply">
              Apply
            </a>
          </article>
        </main>
        """
    )
    executor = _requisition_executor()

    assert executor._discover_application_link(browser_page) == (
        "https://job-boards.greenhouse.io/gsacapital/jobs/8518528002/apply"
    )
    assert executor._apply_hop_evidence["requisition_match"] is True
    assert executor._apply_hop_evidence["role_corroborated"] is False
    assert executor._apply_hop_evidence["employer_corroborated"] is False


@pytest.mark.parametrize(
    "markup",
    (
        """
        <main><article data-requisition="8518528003">
          <a href="https://job-boards.greenhouse.io/acme/jobs/8518528003/apply">Apply</a>
        </article></main>
        """,
        """
        <main>
          <article data-requisition="8518528002"><button>Apply</button></article>
          <article data-requisition="8518528002"><button>Apply</button></article>
        </main>
        """,
        """
        <main><article data-requisition="8518528002" data-job-id="8518528003">
          <button>Apply</button>
        </article></main>
        """,
    ),
)
def test_wrong_duplicate_or_conflicting_requisition_never_binds(
    browser_page: Page,
    markup: str,
) -> None:
    browser_page.set_content(markup)
    executor = _requisition_executor()

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is False


def test_requisition_mutation_after_js_discovery_prevents_click(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article id="job" data-requisition="8518528002">
          <button id="apply" type="button">Apply</button>
        </article></main>
        <script>
          window.clickCount = 0;
          document.querySelector('#apply').addEventListener('click', () => {
            window.clickCount += 1;
          });
        </script>
        """
    )
    executor = _requisition_executor()
    assert executor._discover_application_link(browser_page) == ""
    affordance = executor._discover_apply_affordance(browser_page)
    assert affordance is not None

    browser_page.locator("#job").evaluate(
        "node => node.setAttribute('data-requisition', '8518528003')"
    )
    result = guarded_apply_click(browser_page, affordance, timeout_ms=1_000)

    assert result.clicked is False
    assert result.outcome == "apply_click_requisition_changed"
    assert browser_page.evaluate("window.clickCount") == 0


def test_document_root_binds_only_when_persisted_url_has_exact_requisition(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><button type="button">Apply now</button></main>
        """
    )
    executor = _requisition_executor()

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is True
    assert executor._discover_apply_affordance(browser_page) is not None


class _DestinationPage:
    def __init__(self, url: str, observation: dict[str, object]) -> None:
        self.url = url
        self.observation = observation

    def content(self) -> str:
        return (
            "<html><body><form><input name='email'>"
            "<button type='submit'>Apply</button></form></body></html>"
        )

    def evaluate(self, _script, *_args):  # noqa: ANN001 - page-like test seam
        return dict(self.observation)


def _inspect_destination(
    *,
    final_url: str,
    requisition: str,
    form_visible: bool = True,
    form_action: str | None = None,
) -> _SourceResolutionExecutor:
    executor = _requisition_executor()
    executor._apply_origin_resolution = TargetResolution(
        source_url=executor.source_url,
        final_url=executor.inspection_url,
        kind=TargetKind.JOB_DETAIL,
        provider="greenhouse",
        reason_codes=("direct_ats_job",),
    )
    executor._apply_click_evidence = {"attempted": True, "clicked": True}
    observation = {
        "provider": "greenhouse",
        "requisition": requisition,
        "form_identity": requisition if form_visible else "",
        "root_selector": "[data-argus-form-root='one']" if form_visible else "",
        "form_action": (form_action or final_url) if form_visible else "",
        "control_count": 2 if form_visible else 0,
        "submit_present": form_visible,
        "form_visible": form_visible,
        "application_entry_visible": True,
    }
    executor._inspect(_DestinationPage(final_url, observation))
    return executor


def test_matching_requisition_form_promotes_without_role_or_employer_prose() -> None:
    executor = _inspect_destination(
        final_url="https://job-boards.greenhouse.io/gsacapital/jobs/8518528002",
        requisition="8518528002",
    )

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.APPLICATION_FORM
    assert executor.resolution.verified_for_automation is True
    checks = executor._apply_click_evidence["destination_verification"]
    assert checks["requisition_bound"] is True
    assert checks["employer_bound"] is False
    assert checks["role_bound"] is False


def test_apply_hop_to_different_requisition_fails_closed() -> None:
    executor = _inspect_destination(
        final_url="https://job-boards.greenhouse.io/gsacapital/jobs/8518528003",
        requisition="8518528003",
    )

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert executor._apply_click_evidence["outcome"] == (
        "apply_destination_requisition_mismatch"
    )


def test_form_action_to_different_requisition_fails_closed() -> None:
    executor = _inspect_destination(
        final_url="https://job-boards.greenhouse.io/gsacapital/jobs/8518528002",
        requisition="8518528002",
        form_action=(
            "https://job-boards.greenhouse.io/gsacapital/jobs/8518528003/apply"
        ),
    )

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    checks = executor._apply_click_evidence["destination_verification"]
    assert checks["form_action_requisition_bound"] is False
    assert executor._apply_click_evidence["outcome"] == (
        "apply_destination_form_action_requisition_mismatch"
    )


def test_apply_hop_that_escapes_persisted_origin_fails_closed() -> None:
    executor = _inspect_destination(
        final_url=(
            "https://jobs.lever.co/gsacapital/"
            "9f73569e-952b-4c0a-b77c-d302846f15bd/apply"
        ),
        requisition="8518528002",
    )

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    checks = executor._apply_click_evidence["destination_verification"]
    assert checks["destination_origin_bound"] is False
    assert executor._apply_click_evidence["outcome"] in {
        "apply_destination_provider_mismatch",
        "apply_destination_origin_mismatch",
    }


def test_form_shaped_url_without_form_is_not_application_form() -> None:
    executor = _inspect_destination(
        final_url=(
            "https://job-boards.greenhouse.io/gsacapital/jobs/8518528002/apply"
        ),
        requisition="8518528002",
        form_visible=False,
    )

    assert executor.resolution is not None
    assert executor.resolution.kind is not TargetKind.APPLICATION_FORM
    assert executor._apply_click_evidence["destination_verification"][
        "form_visible"
    ] is False


def test_two_plain_apply_buttons_remain_ambiguous_and_never_click(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main data-requisition="8518528002">
          <button type="button">Apply</button>
          <button type="button">Apply now</button>
        </main>
        """
    )
    executor = _requisition_executor()
    assert executor._discover_application_link(browser_page) == ""

    assert executor._discover_apply_affordance(browser_page) is None
    assert executor._apply_click_evidence["affordance_count"] == 2
    assert executor._apply_click_evidence["outcome"] == (
        "apply_click_multiple_affordances"
    )


def test_http_404_precedes_provider_hint_and_never_promotes() -> None:
    from app.automation.targets import classify_target

    resolution = classify_target(
        "https://app.the-trackr.com/uk-finance/spring-weeks",
        "https://job-boards.greenhouse.io/acme/jobs/8518528002",
        html="<html><form><button type='submit'>Apply</button></form></html>",
        provider_hint="greenhouse",
        http_status=404,
        identity_verified=True,
    )

    assert resolution.kind is TargetKind.BLOCKED
    assert resolution.verified_for_automation is False
    assert resolution.reason_codes == ("http_not_found",)


def test_url_shaped_like_form_without_visible_form_is_not_application_form() -> None:
    class _Page:
        url = "https://job-boards.greenhouse.io/acme/jobs/8518528002/application"

        @staticmethod
        def content() -> str:
            return '<main data-requisition="8518528002"></main>'

        @staticmethod
        def evaluate(_script: str, *_args):  # noqa: ANN001
            return {
                "provider": "greenhouse",
                "requisition": "8518528002",
                "form_visible": False,
                "application_entry_visible": False,
            }

    executor = _requisition_executor()
    executor._inspect(_Page())

    assert executor.resolution is not None
    assert executor.resolution.kind is not TargetKind.APPLICATION_FORM
