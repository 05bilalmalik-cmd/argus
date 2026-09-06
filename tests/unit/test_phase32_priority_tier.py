from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Page, sync_playwright

import app.automation.targets as target_module
from app.automation.targets import TargetResolution
from app.config import Settings
from app.domain.states import ApplicationState, UserApplicationStatus
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, AuditEvent, Opportunity
from app.services.navigator import _SourceResolutionExecutor
from app.services.requisition_identity import requisition_identity_from_url
from app.services.target_resolution import ResolutionContext


@pytest.fixture(scope="module")
def browser_page() -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _embed_url(*, organization: str = "acme", token: str = "8570661002") -> str:
    return (
        "https://job-boards.greenhouse.io/embed/job_app"
        f"?for={organization}&token={token}&gh_src=Trackr"
    )


def _embed_markup(*, organization: str = "acme", token: str = "8570661002", employer: str = "Acme Capital") -> str:
    return f"""
    <!doctype html><html><body>
      <section data-greenhouse-organization="{organization}"
               data-greenhouse-token="{token}"
               data-employer="{employer}"
               data-role="Summer Analyst Internship">
        <h1>Summer Analyst Internship</h1>
        <form id="application-form" method="get">
          <label>Email<input name="email" type="email"></label>
          <button type="submit">Submit application</button>
        </form>
      </section>
    </body></html>
    """


def test_greenhouse_embed_is_a_first_class_verified_application_form() -> None:
    url = _embed_url()
    resolution = target_module.classify_target(
        url,
        url,
        html=_embed_markup(),
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={
            "provider": "greenhouse",
            "employer": "Acme Capital",
            "role": "Summer Analyst Internship",
        },
    )

    assert requisition_identity_from_url(url) is not None
    assert resolution.kind is TargetKind.APPLICATION_FORM
    assert resolution.provider == "greenhouse"
    assert resolution.identity_verified is True
    assert resolution.form_verified is True
    assert resolution.verified_for_automation is True
    assert resolution.evidence["greenhouse_embed"]["organization"] == "acme"
    assert resolution.evidence["greenhouse_embed"]["token"] == "8570661002"


def test_greenhouse_embed_organization_mismatch_is_rejected() -> None:
    url = _embed_url(organization="acme")
    resolution = target_module.classify_target(
        url,
        url,
        html=_embed_markup(organization="other-company"),
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={
            "provider": "greenhouse",
            "employer": "Acme Capital",
            "role": "Summer Analyst Internship",
        },
    )

    assert resolution.kind is TargetKind.MISMATCH
    assert "greenhouse_embed_organization_mismatch" in resolution.reason_codes
    assert resolution.verified_for_automation is False


def _listing_markup(count: int = 2) -> str:
    rows = []
    for index in range(count):
        rows.append(
            f"""
            <article data-job-listing>
              <h2>Goldman Programme {index + 1}</h2>
              <span data-location>London {index + 1}</span>
              <span data-programme-type>Placement</span>
              <a href="https://higher.gs.com/campus/job/{index + 1}">View role</a>
            </article>
            """
        )
    return f'<main data-source-listing>{"".join(rows)}</main>'


def test_search_listing_is_explicit_and_enumerates_all_candidate_roles() -> None:
    url = (
        "https://higher.gs.com/campus?EXPERIENCE_LEVEL=Seasonal"
        '&search=%22one+year%22'
    )
    resolution = target_module.classify_target(url, url, html=_listing_markup())

    assert resolution.kind is TargetKind.MULTIPLE_CANDIDATE_ROLES
    assert resolution.kind is not TargetKind.JOB_DETAIL
    assert resolution.evidence["candidate_count"] == 2
    candidates = resolution.evidence["candidate_roles"]
    assert [item["title"] for item in candidates] == [
        "Goldman Programme 1",
        "Goldman Programme 2",
    ]
    assert candidates[0]["location"] == "London 1"
    assert candidates[0]["programme_type"] == "Placement"
    assert candidates[0]["url"].endswith("/job/1")


def test_one_listing_result_still_requires_explicit_human_choice() -> None:
    url = "https://www.janestreet.com/join-jane-street/open-roles/?type=internship"
    resolution = target_module.classify_target(url, url, html=_listing_markup(1))

    assert resolution.kind is TargetKind.MULTIPLE_CANDIDATE_ROLES
    assert resolution.evidence["candidate_count"] == 1
    assert resolution.evidence["human_choice_required"] is True
    assert target_module.enumerate_candidate_roles(_listing_markup(1), url)


@contextmanager
def _selection_app(
    tmp_path: Path,
    *,
    user_status: UserApplicationStatus = UserApplicationStatus.NOT_APPLIED,
) -> tuple[TestClient, Application, str]:
    app = create_app(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    app.state.ui_v2_enabled = True
    chosen_url = "http://127.0.0.1:8787/lab/ats/greenhouse/application"
    with TestClient(app) as client:
        with client.app.state.db.session_scope() as session:
            opportunity = Opportunity(
                employer="Acme Capital",
                role_title="Summer Analyst Internship",
                programme_group="summer",
                cycle="2027",
                location="London",
                url="http://127.0.0.1:8787/source/listing?id=phase32",
                application_url=None,
                target_status=TargetKind.MULTIPLE_CANDIDATE_ROLES.value,
                user_status=user_status.value,
                application_window_status="OPEN",
                resolution_evidence_json=json.dumps(
                    {
                        "reason_codes": ["candidate_roles_require_human_choice"],
                        "candidate_count": 1,
                        "human_choice_required": True,
                        "candidate_roles": [
                            {
                                "title": "Summer Analyst Internship",
                                "location": "London",
                                "programme_type": "Summer",
                                "url": chosen_url,
                            }
                        ],
                    }
                ),
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.NEEDS_USER.value,
                priority=80,
                risk_level=3,
                next_action="Choose the exact role from the listing",
                eligibility_json=json.dumps({"eligible": True, "reason_codes": []}),
                conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
            )
            session.add(application)
            session.flush()

        def resolver(context: ResolutionContext) -> TargetResolution:
            return TargetResolution(
                source_url=context.source_url,
                final_url=context.inspection_url,
                kind=TargetKind.APPLICATION_ENTRY,
                provider="greenhouse",
                identity_verified=True,
                reason_codes=("phase32_human_choice_test",),
                evidence={
                    "synthetic_lab": True,
                    "provider": "greenhouse",
                    "employer": context.employer,
                    "role": context.role_title,
                    "requisition": "/lab/ats/greenhouse/application",
                    "form_identity": "greenhouse-application",
                    "application_origin": "http://127.0.0.1:8787",
                },
            )

        client.app.state.manual_target_lab_enabled = True
        client.app.state.target_resolution_resolver = resolver
        yield client, application, chosen_url


def test_human_listing_choice_is_stored_and_reruns_identity_checks(tmp_path: Path) -> None:
    with _selection_app(tmp_path) as (client, application, chosen_url):
        response = client.post(
            f"/api/applications/{application.id}/choose-target",
            json={
                "application_id": application.id,
                "application_url": chosen_url,
                "resolution_reason": "I selected the exact role after reviewing the listing.",
                "confirmed": True,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["human_supplied"] is True
        assert payload["promoted"] is True
        assert payload["application_url"] == chosen_url
        assert payload["submitted"] is False

        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.opportunity.application_url == chosen_url
            assert session.query(AuditEvent).filter_by(
                event_type="opportunity.target_resolution_human_supplied"
            ).count() == 1


def test_listing_choice_cannot_accept_a_url_not_in_the_enumeration(tmp_path: Path) -> None:
    with _selection_app(tmp_path) as (client, application, _chosen_url):
        response = client.post(
            f"/api/applications/{application.id}/choose-target",
            json={
                "application_id": application.id,
                "application_url": "http://127.0.0.1:8787/lab/ats/greenhouse/other",
                "resolution_reason": "I selected a candidate role from the listing.",
                "confirmed": True,
            },
        )

        assert response.status_code == 409
        assert "enumerated" in response.json()["detail"]
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.opportunity.application_url is None


def test_excluded_user_status_is_never_offered_listing_choice(tmp_path: Path) -> None:
    with _selection_app(
        tmp_path,
        user_status=UserApplicationStatus.APPLICATION_SUBMITTED,
    ) as (client, application, chosen_url):
        response = client.post(
            f"/api/applications/{application.id}/choose-target",
            json={
                "application_id": application.id,
                "application_url": chosen_url,
                "resolution_reason": "I reviewed the exact role listing again.",
                "confirmed": True,
            },
        )
        assert response.status_code == 409
        assert UserApplicationStatus.APPLICATION_SUBMITTED.value in response.json()["detail"]

        page = client.get("/needs-you?group=blocked")
        assert page.status_code == 200
        assert 'name="application_url"' not in page.text
        assert "No action required" in page.text


def test_existing_human_url_path_accepts_pre_submission_not_yet_open_target(
    tmp_path: Path,
) -> None:
    with _selection_app(tmp_path) as (client, application, chosen_url):
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            persisted.state = ApplicationState.DISCOVERED.value
            persisted.opportunity.target_status = TargetKind.MISSING_EMPLOYER_LINK.value
            persisted.opportunity.application_window_status = "NOT_YET_OPEN"

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": chosen_url,
                "resolution_reason": "I verified the official target and timing.",
                "confirmed": True,
            },
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["human_supplied"] is True
        assert payload["promoted"] is False
        assert payload["submitted"] is False
        assert payload["verification"]["status"] == "recorded_not_open"
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.opportunity.application_url == chosen_url


def _apply_executor() -> _SourceResolutionExecutor:
    return _SourceResolutionExecutor(
        source_url="https://jobs.exxonmobil.com/job/example/1422465900/",
        inspection_url="https://jobs.exxonmobil.com/job/example/1422465900/",
        provider_hint="unknown",
        employer="ExxonMobil",
        role_title="Commercial Industrial Placement - Global Trading - London",
        apply_click_enabled=True,
    )


def test_styled_link_apply_control_is_found_inside_bound_job_root(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <div class="jobDisplay">
          <div class="jobTitle">
            <h1>Commercial Industrial Placement - Global Trading - London</h1>
            <p>ExxonMobil</p>
            <a class="btn btn-primary apply dialogApplyBtn"
               href="/talentcommunity/apply/1422465900/?locale=en_US">Apply now »</a>
          </div>
        </div>
        """
    )
    affordance = _apply_executor()._discover_apply_affordance(
        browser_page,
        apply_click_identity_only=True,
    )

    assert affordance is not None
    assert affordance.accessible_name == "Apply now »"
    assert affordance.role == "link"


def test_late_apply_control_is_found_with_bounded_observation(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <div class="jobDisplay">
          <h1>Commercial Industrial Placement - Global Trading - London</h1>
          <p>ExxonMobil</p>
          <div id="apply-mount"></div>
        </div>
        <script>
          setTimeout(() => {
            document.querySelector('#apply-mount').innerHTML =
              '<a href="/talentcommunity/apply/1422465900/">Apply now</a>';
          }, 50);
        </script>
        """
    )
    affordance = _apply_executor()._discover_apply_affordance(
        browser_page,
        apply_click_identity_only=True,
    )

    assert affordance is not None
    assert affordance.accessible_name == "Apply now"


def test_apply_control_inside_same_origin_frame_is_found_without_clicking(browser_page: Page) -> None:
    browser_page.set_content('<iframe id="apply-frame"></iframe>')
    frame = browser_page.frames[-1]
    frame.set_content(
        """
        <div data-job-listing data-employer="ExxonMobil"
             data-role="Commercial Industrial Placement - Global Trading - London">
          <h1>Commercial Industrial Placement - Global Trading - London</h1>
          <a href="/talentcommunity/apply/1422465900/">Apply now</a>
        </div>
        """
    )
    affordance = _apply_executor()._discover_apply_affordance(
        browser_page,
        apply_click_identity_only=True,
    )

    assert affordance is not None
    assert affordance.accessible_name == "Apply now"


def test_phase32_keeps_apply_allowlist_denylist_and_no_submit_authority() -> None:
    assert target_module.trusted_provider_for_url(_embed_url()) == "greenhouse"
    assert target_module.trusted_provider_for_url(
        "https://greenhouse.io.attacker.test/embed/job_app?for=acme&token=8570661002"
    ) == ""
    from app.services.resolution_apply_click import is_explicit_apply_name

    assert is_explicit_apply_name("Apply") is True
    assert is_explicit_apply_name("Apply now") is True
    for value in ("Submit", "Send", "Confirm", "Withdraw", "Pay application fee", "Next"):
        assert is_explicit_apply_name(value) is False
