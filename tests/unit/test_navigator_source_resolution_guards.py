"""Focused guards for visible source discovery and trusted ATS egress."""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from app.automation.types import RunMode
from app.automation.types import SessionState
from app.automation.targets import TargetResolution
from app.automation.host_policy import origin_for_url
from app.domain.targets import TargetKind
from app.services.navigator import (
    ApplicationNavigator,
    DuplicateSessionError,
    HeadedSessionWorker,
    _ManagedSession,
    _SourceResolutionExecutor,
    _utcnow,
)


class _Page:
    def __init__(self, *, href: str, count: int = 1) -> None:
        self.url = "http://127.0.0.1:8787/listing"
        self.href = href
        self.count = count
        self.goto_calls: list[tuple[str, str, int]] = []

    def evaluate(self, _script, _args=None):  # noqa: ANN001
        # Discovery and page identity are deliberately separate owner-thread
        # reads; this fake only answers the discovery contract.
        if "const candidates" in _script:
            return {"href": self.href if self.count == 1 else "", "count": self.count}
        return {}

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url


def _executor() -> _SourceResolutionExecutor:
    return _SourceResolutionExecutor(
        source_url="http://127.0.0.1:8787/listing",
        provider_hint="greenhouse",
        employer="Example Employer",
        role_title="Analyst",
    )


def test_source_discovery_follows_one_same_origin_application_link_without_clicking():
    page = _Page(href="http://127.0.0.1:8787/application")
    executor = _executor()

    assert executor._follow_discovered_application(page) is True
    assert page.goto_calls == [
        ("http://127.0.0.1:8787/application", "domcontentloaded", 10_000)
    ]
    # Discovery is bounded to one owner-thread navigation.
    assert executor._follow_discovered_application(page) is False


def test_source_discovery_refuses_ambiguous_application_links():
    page = _Page(href="http://127.0.0.1:8787/application", count=2)
    assert _executor()._follow_discovered_application(page) is False
    assert page.goto_calls == []


def test_listing_apply_link_is_followed_only_after_explicit_handoff_resume():
    """Keep the established human-handoff boundary for generic listings."""

    page = _Page(href="http://127.0.0.1:8787/application")
    executor = _executor()

    executor.prepare(page)
    assert page.goto_calls == []
    assert executor.resolution is not None
    initial_kind = executor.resolution.kind

    executor.resume(page)

    assert page.goto_calls == [
        ("http://127.0.0.1:8787/application", "domcontentloaded", 10_000)
    ]
    assert executor.resolution is not None
    assert executor.resolution.kind is initial_kind
    assert executor.resolution.evidence["apply_hop"]["outcome"] == (
        "destination_not_application_form"
    )


class _ApplyHopPage:
    source_url = "https://careers.example.test/jobs/analyst"
    form_url = "https://job-boards.greenhouse.io/example/jobs/1234"

    def __init__(
        self,
        *,
        href: str | None = None,
        candidates: tuple[str, ...] | None = None,
        second_hop: str = "",
    ) -> None:
        self.url = self.source_url
        self.href = href or self.form_url
        self.candidates = candidates or (self.href,)
        self.second_hop = second_hop
        self.goto_calls: list[tuple[str, str, int]] = []
        self.click_calls: list[str] = []
        self.discovery_reads = 0

    def content(self) -> str:
        if self.url == self.form_url:
            return (
                '<main><form id="apply" method="post" action="'
                f'{self.form_url}"><input name="first_name">'
                '<button type="submit">Submit Application</button></form></main>'
            )
        return "<main><h1>Analyst</h1><p>Example Employer</p></main>"

    def evaluate(self, script, *_args):
        if "const candidates" in script:
            self.discovery_reads += 1
            candidates = self.candidates
            if self.goto_calls and self.second_hop:
                candidates = (self.second_hop,)
            return {
                "href": candidates[0] if len(candidates) == 1 else "",
                "count": len(candidates),
                "candidate_urls": list(candidates),
            }
        if "const root = first" in script and self.url == self.form_url:
            return {
                "provider": "greenhouse",
                "employer": "Example Employer",
                "role": "Analyst",
                "requisition": "1234",
                "form_identity": "apply",
                "root_selector": "#apply",
                "form_action": self.form_url,
                "form_method": "POST",
                "control_count": 2,
                "submit_present": True,
                "form_visible": True,
                "application_entry_visible": True,
            }
        return {}

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url

    def click(self, selector: str) -> None:
        self.click_calls.append(selector)
        pytest.fail("source resolution must never click an Apply or Submit control")


class _UnboundApplyPage(_ApplyHopPage):
    def evaluate(self, script, *_args):
        if "const candidates" in script:
            self.discovery_reads += 1
            return {
                "href": "",
                "count": 0,
                "control_count": 0,
                "visible_root_count": 2,
                "bound_job_root_found": False,
                "page_apply_affordance_count": 1,
                "bound_apply_affordance_count": 0,
                "candidate_urls": [],
            }
        return {}


class _EmployerMismatchPage(_ApplyHopPage):
    def evaluate(self, script, *_args):
        result = super().evaluate(script, *_args)
        if "const root = first" in script and self.url == self.form_url:
            result = dict(result)
            result["employer"] = "Different Employer"
        return result


class _SourceIdentityMismatchPage:
    url = (
        "https://job-boards.greenhouse.io/example/jobs/"
        "1234&gh_src=Trackr"
    )

    @staticmethod
    def content() -> str:
        return (
            '<main><form id="apply" method="post" action="'
            f'{_SourceIdentityMismatchPage.url}"><input name="first_name">'
            '<button type="submit">Submit Application</button></form></main>'
        )

    @staticmethod
    def evaluate(script, *_args):
        if "const candidates" in script:
            return {
                "href": "",
                "count": 0,
                "control_count": 0,
                "visible_root_count": 1,
                "bound_job_root_found": True,
                "page_apply_affordance_count": 1,
                "bound_apply_affordance_count": 1,
                "candidate_urls": [],
            }
        if "const root = first" in script:
            return {
                "provider": "greenhouse",
                "employer": "Different Employer",
                "role": "Analyst",
                "requisition": "1234",
                "form_identity": "apply",
                "root_selector": "#apply",
                "form_action": _SourceIdentityMismatchPage.url,
                "form_method": "POST",
                "control_count": 2,
                "submit_present": True,
                "form_visible": True,
                "application_entry_visible": True,
            }
        return {}


def _apply_executor() -> _SourceResolutionExecutor:
    return _SourceResolutionExecutor(
        source_url=_ApplyHopPage.source_url,
        provider_hint="unknown",
        employer="Example Employer",
        role_title="Analyst",
        allowlist=frozenset({"careers.example.test"}),
    )


def test_job_detail_apply_link_is_followed_once_to_verified_application_form():
    page = _ApplyHopPage()
    executor = _apply_executor()

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.APPLICATION_FORM
    assert executor.resolution.form_verified is True
    assert page.goto_calls == [
        (_ApplyHopPage.form_url, "domcontentloaded", 10_000)
    ]
    assert page.click_calls == []
    hop = executor.resolution.evidence["apply_hop"]
    assert tuple(hop["hop_chain"]) == (
        _ApplyHopPage.source_url,
        _ApplyHopPage.form_url,
    )
    assert hop["outcome"] == "verified_application_form"


def test_job_detail_apply_follow_is_capped_at_one_navigation():
    first_hop = "https://careers.example.test/jobs/analyst/apply-step"
    page = _ApplyHopPage(
        href=first_hop,
        second_hop=_ApplyHopPage.form_url,
    )
    executor = _apply_executor()

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert page.goto_calls == [(first_hop, "domcontentloaded", 10_000)]
    assert page.discovery_reads == 1
    hop = executor.resolution.evidence["apply_hop"]
    assert hop["outcome"] == "destination_not_application_form"


def test_conflicting_apply_controls_stay_job_detail_with_attempt_evidence():
    page = _ApplyHopPage(
        candidates=(
            "https://job-boards.greenhouse.io/example/jobs/1234",
            "https://jobs.lever.co/example/analyst",
        )
    )
    executor = _apply_executor()

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert page.goto_calls == []
    hop = executor.resolution.evidence["apply_hop"]
    assert hop["outcome"] == "ambiguous_apply_controls"
    assert hop["candidate_count"] == 2


def test_missing_bound_job_root_is_persistable_as_a_specific_apply_reason():
    page = _UnboundApplyPage()
    executor = _apply_executor()

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_bound_job_root_not_found" in executor.resolution.reason_codes
    hop = executor.resolution.evidence["apply_hop"]
    assert hop["outcome"] == "apply_bound_job_root_not_found"
    assert hop["visible_root_count"] == 2
    assert hop["bound_job_root_found"] is False
    assert hop["page_apply_affordance_count"] == 1
    assert hop["bound_apply_affordance_count"] == 0
    assert hop["eligible_destination_count"] == 0
    assert hop["ambiguity_guard_triggered"] is False
    assert hop["origin_binding"] == "not_reached"
    assert hop["egress_guard"] == "not_reached"
    assert hop["destination_verification"] == "not_reached"


def test_apply_destination_records_the_exact_identity_check_that_failed():
    page = _EmployerMismatchPage()
    executor = _apply_executor()

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_destination_employer_mismatch" in executor.resolution.reason_codes
    hop = executor.resolution.evidence["apply_hop"]
    assert hop["outcome"] == "apply_destination_employer_mismatch"
    assert hop["origin_binding"] == "passed"
    checks = hop["destination_verification"]
    assert checks["reached"] is True
    assert checks["provider_verified"] is True
    assert checks["employer_observed"] is True
    assert checks["employer_bound"] is False
    assert checks["role_observed"] is True
    assert checks["role_bound"] is True
    assert checks["requisition_observed"] is True
    assert checks["form_identity_observed"] is True
    assert checks["form_visible"] is True
    assert checks["form_action_origin_bound"] is True


def test_source_form_identity_mismatch_is_recorded_without_relaxing_verification():
    page = _SourceIdentityMismatchPage()
    executor = _SourceResolutionExecutor(
        source_url=page.url,
        provider_hint="greenhouse",
        employer="Example Employer",
        role_title="Analyst",
        allowlist=frozenset({"job-boards.greenhouse.io"}),
    )

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert executor.resolution.identity_verified is False
    assert "apply_affordance_has_no_get_destination" in (
        executor.resolution.reason_codes
    )
    checks = executor.resolution.evidence["apply_hop"]["source_verification"]
    assert checks["reached"] is True
    assert checks["provider_verified"] is True
    assert checks["employer_observed"] is True
    assert checks["employer_bound"] is False
    assert checks["role_observed"] is True
    assert checks["role_bound"] is True
    assert checks["requisition_observed"] is True
    assert checks["form_identity_observed"] is True
    assert checks["form_visible"] is True


def test_off_origin_apply_url_must_be_bound_by_the_page_discovery_read():
    page = _ApplyHopPage()
    executor = _apply_executor()

    assert executor._approved_discovery_url(page, _ApplyHopPage.form_url) == ""


def test_apply_link_open_redirect_laundering_is_refused():
    redirect = (
        "https://careers.example.test/redirect?next="
        "https%3A%2F%2Fjob-boards.greenhouse.io%2Fexample%2Fjobs%2F1234"
    )
    page = _ApplyHopPage(href=redirect)
    executor = _apply_executor()

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert page.goto_calls == []
    assert executor.resolution.evidence["apply_hop"]["outcome"] == (
        "open_redirect_refused"
    )


class _MissingStructuredIdentityPage:
    """A loopback form whose URL looks useful but DOM identity is absent."""

    url = "http://127.0.0.1:8787/application"

    def content(self) -> str:
        return (
            '<main data-ats="greenhouse" data-employer="Example Employer" '
            'data-role="Analyst"><form id="apply"><button type="submit">Apply</button>'
            "</form></main>"
        )

    def evaluate(self, script, *_args):  # noqa: ANN001
        if "const root = first" not in script:
            return {}
        return {
            "provider": "greenhouse",
            "employer": "Example Employer",
            "role": "Analyst",
            # Deliberately omit both structured identity fields.  The URL
            # path must not be promoted into requisition/form proof.
            "root_selector": "#apply",
            "form_action": "http://127.0.0.1:8787/submit",
            "form_method": "POST",
            "control_count": 1,
            "submit_present": True,
            "form_visible": True,
        }


def test_source_inspection_rejects_url_only_requisition_and_form_identity():
    executor = _SourceResolutionExecutor(
        source_url="http://127.0.0.1:8787/application",
        provider_hint="greenhouse",
        employer="Example Employer",
        role_title="Analyst",
    )

    executor.prepare(_MissingStructuredIdentityPage())

    assert executor.resolution is not None
    assert executor.resolution.verified_for_automation is False
    assert any("unverified" in reason for reason in executor.resolution.reason_codes)


class _StructuredWorkdayJobPage:
    url = (
        "https://gresearch.wd103.myworkdayjobs.com/en-US/G-Research/"
        "job/Quant-Research-Internship_R36918"
    )

    def content(self) -> str:
        return """
        <!doctype html><html><body><main>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Quant Research Internship",
            "identifier": {"@type": "PropertyValue", "value": "R36918"},
            "hiringOrganization": {"@type": "Organization", "name": "G-Research"}
          }
          </script>
          <button data-automation-id="applyButton">Apply</button>
        </main></body></html>
        """

    def evaluate(self, script, *_args):  # noqa: ANN001
        if "const root = first" not in script:
            return {}
        return {
            "form_identity": "workday:applyButton",
            "application_entry_visible": True,
            "form_visible": False,
            "control_count": 0,
            "submit_present": False,
        }


def test_workday_jobposting_and_visible_apply_entry_produce_verified_target():
    executor = _SourceResolutionExecutor(
        source_url=_StructuredWorkdayJobPage.url,
        provider_hint="workday",
        employer="G-Research",
        role_title="Quant Research Internship",
    )

    executor.prepare(_StructuredWorkdayJobPage())

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.APPLICATION_ENTRY
    assert executor.resolution.provider == "workday"
    assert executor.resolution.identity_verified is True
    assert executor.resolution.verified_for_automation is True
    assert executor.resolution.evidence["requisition"] == "R36918"


def test_workday_adventure_button_is_observed_as_application_entry():
    executor = _SourceResolutionExecutor(
        source_url=_StructuredWorkdayJobPage.url,
        provider_hint="workday",
        employer="G-Research",
        role_title="Quant Research Internship",
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<main><button data-automation-id="adventureButton">Apply</button></main>'
        )
        observation = executor._page_observation(page)
        browser.close()

    assert observation["application_entry_visible"] is True
    assert observation["form_identity"] == "application-entry:adventureButton"


def test_current_greenhouse_react_form_binds_page_authored_job_identity():
    """The current Greenhouse board bootstrap must bind its visible form.

    This catches a regression where ARGUS understands only JSON-LD or legacy
    ``data-*`` markers and repeatedly hands a directly visible application
    form back to the user as unresolved.
    """

    url = (
        "https://job-boards.greenhouse.io/point72/jobs/8423978002"
        "?gh_jid=8423978002"
    )
    markup = """
    <!doctype html><html><head>
      <title>Job Application for 2026 Warsaw MI Data - Web Scraping Internship at Point72</title>
    </head><body>
      <main class="main font-secondary job-post">
        <img alt="Point72 Logo" src="logo.png">
        <h1 class="section-header section-header--large font-primary">
          2026 Warsaw MI Data - Web Scraping Internship
        </h1>
        <button type="button" aria-label="Apply">Apply</button>
        <form method="get"
              action="/point72/jobs/8423978002?gh_jid=8423978002"
              id="application-form">
          <label>First Name<input name="first_name" required></label>
          <label>Email<input name="email" type="email" required></label>
          <button type="submit">Submit Application</button>
        </form>
      </main>
      <script>
        window.__remixContext = {state: {loaderData: {
          root: {boardConfiguration: {disable_captcha: false}},
          "routes/$url_token_.jobs_.$job_post_id": {
            jobPost: {
              company_name: "Point72 ",
              title: "2026 Warsaw MI Data - Web Scraping Internship"
            },
            jobPostId: "8423978002"
          }
        }}};
      </script>
      <div class="grecaptcha-badge" style="display:block">
        <iframe title="recaptcha" src="https://www.google.com/recaptcha/api2/anchor"></iframe>
      </div>
      <textarea class="g-recaptcha-response" style="display:none"></textarea>
    </body></html>
    """
    executor = _SourceResolutionExecutor(
        source_url=url,
        provider_hint="greenhouse",
        employer="point72",
        role_title="2026 Warsaw MI Data - Web Scraping Internship",
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("https://job-boards.greenhouse.io/**", lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=markup,
        ))
        page.goto(url)
        executor.prepare(page)
        browser.close()

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.APPLICATION_FORM
    assert executor.resolution.provider == "greenhouse"
    assert executor.resolution.identity_verified is True
    assert executor.resolution.form_verified is True
    assert executor.resolution.evidence["employer"].strip() == "Point72"
    assert executor.resolution.evidence["requisition"] == "8423978002"
    assert executor.resolution.evidence["form_identity"] == "application-form"


class _Request:
    def __init__(
        self,
        *,
        url: str,
        method: str = "GET",
        resource_type: str = "document",
        post_data: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.url = url
        self.method = method
        self.post_data = post_data
        self.headers = headers or {"accept": "text/html"}
        self.resource_type = resource_type


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self) -> None:
        self.continued += 1

    def abort(self, reason: str) -> None:
        self.aborted.append(reason)


def _worker() -> HeadedSessionWorker:
    worker = HeadedSessionWorker(
        session_id="source-egress",
        application_id="application-1",
        mode=RunMode.REVIEW.value,
        url="http://127.0.0.1:8787/listing",
        summary={"source_resolution": True, "provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=False,
    )
    worker.owner_thread_id = threading.get_ident()
    return worker


def test_source_resolution_allows_only_exact_trusted_provider_document_get():
    route = _Route(_Request(url="https://boards.greenhouse.io/example/jobs/123"))
    _worker()._route(route)
    assert route.continued == 1
    assert route.aborted == []


def test_source_resolution_rejects_trusted_provider_post_and_untrusted_document():
    post = _Route(
        _Request(
            url="https://boards.greenhouse.io/example/jobs/123",
            method="POST",
        )
    )
    _worker()._route(post)
    assert post.continued == 0
    assert post.aborted == ["blockedbyclient"]

    untrusted = _Route(_Request(url="https://attacker.example/jobs/123"))
    _worker()._route(untrusted)
    assert untrusted.continued == 0
    assert untrusted.aborted == ["blockedbyclient"]


def test_route_without_optional_journey_executor_remains_fail_closed():
    worker = _worker()
    del worker.journey_executor
    route = _Route(_Request(url="https://attacker.example/jobs/123"))

    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]


def test_apply_hop_records_the_specific_egress_guard_refusal():
    executor = _apply_executor()
    executor._apply_hop_target_url = _ApplyHopPage.form_url
    executor._apply_hop_evidence = {
        "attempted": True,
        "outcome": "navigation_started",
    }
    worker = _worker()
    worker.journey_executor = executor
    route = _Route(
        _Request(url="https://job-boards.greenhouse.io/example/jobs/not-bound")
    )

    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert executor._apply_hop_evidence["egress_guard"] == {
        "reached": True,
        "allowed": False,
        "fatal": True,
        "reason": "source_resolution_apply_navigation_not_bound",
    }


def test_greenhouse_prefill_allows_only_exact_read_only_runtime_assets():
    worker = HeadedSessionWorker(
        session_id="greenhouse-assets",
        application_id="application-1",
        mode=RunMode.PREFILL.value,
        url="https://job-boards.greenhouse.io/point72/jobs/8423978002",
        summary={"provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=False,
    )
    worker.owner_thread_id = threading.get_ident()

    asset = _Route(
        _Request(
            url="https://job-boards.cdn.greenhouse.io/assets/entry.js",
            resource_type="script",
        )
    )
    worker._route(asset)
    assert asset.continued == 1
    assert asset.aborted == []

    locale = _Route(
        _Request(
            url="https://job-boards.cdn.greenhouse.io/locales/en/common.hash.json",
            resource_type="fetch",
        )
    )
    worker._route(locale)
    assert locale.continued == 1
    assert locale.aborted == []

    post = _Route(
        _Request(
            url="https://job-boards.cdn.greenhouse.io/assets/entry.js",
            method="POST",
            resource_type="fetch",
        )
    )
    worker._route(post)
    assert post.continued == 0
    assert post.aborted == ["blockedbyclient"]

    arbitrary_json = _Route(
        _Request(
            url="https://job-boards.cdn.greenhouse.io/export/candidates.json",
            resource_type="fetch",
        )
    )
    worker._route(arbitrary_json)
    assert arbitrary_json.continued == 0
    assert arbitrary_json.aborted == ["blockedbyclient"]

    lookalike = _Route(
        _Request(
            url="https://job-boards.cdn.greenhouse.io.attacker.example/assets/entry.js",
            resource_type="script",
        )
    )
    worker._route(lookalike)
    assert lookalike.continued == 0
    assert lookalike.aborted == ["blockedbyclient"]


def test_greenhouse_runtime_allows_only_exact_presigned_field_bootstrap():
    worker = HeadedSessionWorker(
        session_id="greenhouse-presigned-fields",
        application_id="application-1",
        mode=RunMode.PREFILL.value,
        url="https://job-boards.greenhouse.io/point72/jobs/8423978002",
        summary={"provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=False,
    )
    worker.owner_thread_id = threading.get_ident()

    bootstrap = _Route(
        _Request(
            url=(
                "https://boards.greenhouse.io/uncacheable_attributes/"
                "presigned_fields?fields%5B%5D=resume&fields%5B%5D=cover_letter"
            ),
            resource_type="fetch",
            headers={
                "accept": "*/*",
                "origin": "https://job-boards.greenhouse.io",
            },
        )
    )
    worker._route(bootstrap)
    assert bootstrap.continued == 1
    assert bootstrap.aborted == []

    extra_field = _Route(
        _Request(
            url=(
                "https://boards.greenhouse.io/uncacheable_attributes/"
                "presigned_fields?fields%5B%5D=resume&fields%5B%5D=ssn"
            ),
            resource_type="fetch",
            headers={
                "accept": "*/*",
                "origin": "https://job-boards.greenhouse.io",
            },
        )
    )
    worker._route(extra_field)
    assert extra_field.continued == 0
    assert extra_field.aborted == ["blockedbyclient"]

    post = _Route(
        _Request(
            url=(
                "https://boards.greenhouse.io/uncacheable_attributes/"
                "presigned_fields?fields%5B%5D=resume"
            ),
            method="POST",
            resource_type="fetch",
            headers={
                "content-type": "application/json",
                "origin": "https://job-boards.greenhouse.io",
            },
        )
    )
    worker._route(post)
    assert post.continued == 0
    assert post.aborted == ["blockedbyclient"]

    lookalike = _Route(
        _Request(
            url=(
                "https://boards.greenhouse.io.attacker.example/"
                "uncacheable_attributes/presigned_fields?fields%5B%5D=resume"
            ),
            resource_type="fetch",
            headers={
                "accept": "*/*",
                "origin": "https://job-boards.greenhouse.io",
            },
        )
    )
    worker._route(lookalike)
    assert lookalike.continued == 0
    assert lookalike.aborted == ["blockedbyclient"]


def test_greenhouse_prefill_upload_is_bound_to_the_active_approved_document():
    worker = HeadedSessionWorker(
        session_id="greenhouse-upload",
        application_id="application-1",
        mode=RunMode.PREFILL.value,
        url="https://job-boards.greenhouse.io/point72/jobs/8423978002",
        summary={"provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=False,
        journey_executor=SimpleNamespace(
            active_document_upload={
                "approved": True,
                "kind": "document.cv",
                "path": r"C:\approved\approved-cv.docx",
                "sha256": "a" * 64,
            }
        ),
    )
    worker.owner_thread_id = threading.get_ident()
    multipart = (
        '------argus\r\nContent-Disposition: form-data; name="file"; '
        'filename="approved-cv.docx"\r\n'
        'Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n'
    )
    upload = _Route(
        _Request(
            url="https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/",
            method="POST",
            resource_type="xhr",
            post_data=multipart,
            headers={
                "content-type": "multipart/form-data; boundary=----argus",
                "origin": "https://job-boards.greenhouse.io",
            },
        )
    )
    worker._route(upload)
    assert upload.continued == 1
    assert upload.aborted == []

    wrong_file = _Route(
        _Request(
            url="https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/",
            method="POST",
            resource_type="xhr",
            post_data=multipart.replace("approved-cv.docx", "other.docx"),
            headers={
                "content-type": "multipart/form-data; boundary=----argus",
                "origin": "https://job-boards.greenhouse.io",
            },
        )
    )
    worker._route(wrong_file)
    assert wrong_file.continued == 0
    assert wrong_file.aborted == ["blockedbyclient"]


class _CaptureWorker:
    instances: list["_CaptureWorker"] = []

    def __init__(self, **kwargs):
        self.__class__.instances.append(self)
        self.session_id = kwargs["session_id"]
        self.headless = kwargs["headless"]
        self.cleanup_complete = True
        self.cleanup_escalated = False
        self.owner_thread_id = None
        self.allowlist = kwargs["allowlist"]

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class _EscalatedDeadWorker:
    cleanup_complete = False
    cleanup_escalated = True
    owner_thread_id = 123

    def is_alive(self) -> bool:
        return False


def _verified_source_resolution() -> TargetResolution:
    target = "http://127.0.0.1:8787/application"
    return TargetResolution(
        source_url=target,
        final_url=target,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="loopback",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": "loopback",
            "application_origin": origin_for_url(target),
            "employer": "Example Employer",
            "role": "Analyst",
            "requisition": "/application",
            "form_identity": "application",
        },
    )


def test_source_resolution_blocks_promotion_when_cleanup_escalated():
    navigator = ApplicationNavigator(
        ttl_seconds=0.01,
        worker_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not create a second source owner")
        ),
    )
    now = _utcnow()
    record = _ManagedSession(
        session_id="source-escalated",
        application_id="application-escalated",
        mode=RunMode.REVIEW.value,
        created_at=now,
        updated_at=now,
        expires_at=now,
        deadline_monotonic=0.0,
        summary={"source_resolution": True},
        state=SessionState.FAILED,
        reason="teardown remained incomplete",
        worker=_EscalatedDeadWorker(),
    )
    navigator._sessions[record.session_id] = record
    navigator._source_executors[record.session_id] = SimpleNamespace(
        resolution=_verified_source_resolution()
    )
    navigator._source_context = lambda _application_id: {
        "source_url": "http://127.0.0.1:8787/listing",
        "provider_hint": "loopback",
        "employer": "Example Employer",
        "role_title": "Analyst",
        "opportunity_id": "opportunity-escalated",
    }

    try:
        resolution, handoff = navigator.resolve_application_target(
            "application-escalated"
        )
    finally:
        navigator._shutdown = True

    assert resolution is None
    assert "cleanup" in str(handoff["reason"]).casefold()
    assert "blocked" in str(handoff["reason"]).casefold()


def test_apply_start_refuses_escalated_source_owner_until_cleanup_complete():
    navigator = ApplicationNavigator(
        url_resolver=lambda _application_id: (_verified_source_resolution(), {}),
        worker_factory=lambda **_kwargs: pytest.fail(
            "apply must not create a worker while source teardown is incomplete"
        ),
    )
    now = datetime.now(timezone.utc)
    record = _ManagedSession(
        session_id="apply-escalated",
        application_id="application-escalated-apply",
        mode=RunMode.REVIEW.value,
        created_at=now,
        updated_at=now,
        expires_at=now,
        deadline_monotonic=0.0,
        summary={"source_resolution": True},
        state=SessionState.FAILED,
        reason="teardown remained incomplete",
        worker=_EscalatedDeadWorker(),
    )
    navigator._sessions[record.session_id] = record

    try:
        with pytest.raises(DuplicateSessionError, match="incomplete teardown"):
            navigator.start("application-escalated-apply", RunMode.REVIEW)
    finally:
        navigator._shutdown = True


def test_review_extends_allowlist_only_to_exact_verified_ats_host():
    target = "https://boards.greenhouse.io/example/jobs/123"
    resolution = TargetResolution(
        source_url=target,
        final_url=target,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "provider": "greenhouse",
            "application_origin": origin_for_url(target),
            "employer": "Example Employer",
            "role": "Analyst",
            "requisition": "/example/jobs/123",
            "form_identity": "123",
        },
    )
    _CaptureWorker.instances.clear()
    navigator = ApplicationNavigator(
        settings=SimpleNamespace(live_domain_allowlist=frozenset()),
        headless=True,
        worker_factory=_CaptureWorker,
        url_resolver=lambda _application_id: (resolution, {}),
    )

    snapshot = navigator.start("review-ats", RunMode.REVIEW)

    assert snapshot.headed is True
    assert "boards.greenhouse.io" in _CaptureWorker.instances[-1].allowlist
