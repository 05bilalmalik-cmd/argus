from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import queue
import threading

import pytest

from app.config import Settings
from app.domain.targets import TargetKind


def test_apply_click_feature_defaults_off_with_small_bounded_defaults(
    tmp_path: Path,
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})

    assert settings.apply_click_enabled is False
    assert settings.apply_click_run_cap == 25
    assert settings.apply_click_timeout_ms == 10_000


def test_apply_click_feature_requires_explicit_opt_in(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_APPLY_CLICK": "true",
            "ARGUS_APPLY_CLICK_RUN_CAP": "7",
            "ARGUS_APPLY_CLICK_TIMEOUT_SECONDS": "3.5",
        }
    )

    assert settings.apply_click_enabled is True
    assert settings.apply_click_run_cap == 7
    assert settings.apply_click_timeout_ms == 3_500


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ARGUS_APPLY_CLICK_RUN_CAP", "0"),
        ("ARGUS_APPLY_CLICK_RUN_CAP", "not-an-integer"),
        ("ARGUS_APPLY_CLICK_TIMEOUT_SECONDS", "0"),
        ("ARGUS_APPLY_CLICK_TIMEOUT_SECONDS", "nan"),
    ],
)
def test_apply_click_bounds_reject_invalid_values(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=name):
        Settings.load(
            {
                "ARGUS_DATA_DIR": str(tmp_path),
                name: value,
            }
        )


def test_apply_click_budget_is_a_thread_safe_hard_cap() -> None:
    from app.services.resolution_apply_click import ApplyClickBudget

    budget = ApplyClickBudget(3)
    with ThreadPoolExecutor(max_workers=12) as executor:
        grants = list(executor.map(lambda _index: budget.try_consume(), range(50)))

    assert grants.count(True) == 3
    assert grants.count(False) == 47
    assert budget.consumed == 3
    assert budget.remaining == 0


class _GuardedClickLocator:
    def __init__(
        self,
        *,
        name: str,
        role: str = "button",
        count: int = 1,
        control_type: str = "button",
    ) -> None:
        self.name = name
        self.role = role
        self._count = count
        self.control_type = control_type
        self.click_calls: list[int] = []

    def count(self) -> int:
        return self._count

    def evaluate(self, _script: str) -> dict[str, str]:
        return {
            "accessible_name": self.name,
            "role": self.role,
            "type": self.control_type,
        }

    def click(self, *, timeout: int) -> None:
        self.click_calls.append(timeout)


class _GuardedClickPage:
    def __init__(self, locator: _GuardedClickLocator) -> None:
        self._locator = locator
        self.selectors: list[str] = []

    def locator(self, selector: str) -> _GuardedClickLocator:
        self.selectors.append(selector)
        return self._locator


@pytest.mark.parametrize(
    "dangerous_name",
    [
        "Submit",
        "Submit application",
        "Send",
        "Confirm",
        "Finish",
        "Continue",
        "Continue to next page",
        "Next",
    ],
)
def test_guarded_click_revalidates_and_refuses_submit_or_progress_controls(
    dangerous_name: str,
) -> None:
    from app.services.resolution_apply_click import (
        ApplyAffordance,
        guarded_apply_click,
    )

    locator = _GuardedClickLocator(name=dangerous_name)
    page = _GuardedClickPage(locator)

    result = guarded_apply_click(
        page,
        ApplyAffordance(
            selector='[data-argus-resolution-affordance="one"]',
            accessible_name="Apply now",
            role="button",
        ),
        timeout_ms=2_500,
    )

    assert result.clicked is False
    assert result.outcome == "apply_click_submit_denylist_refused"
    assert result.accessible_name == dangerous_name
    assert result.role == "button"
    assert locator.click_calls == []


def test_guarded_click_allows_exactly_one_revalidated_apply_control() -> None:
    from app.services.resolution_apply_click import (
        ApplyAffordance,
        guarded_apply_click,
    )

    locator = _GuardedClickLocator(name="Apply now")
    page = _GuardedClickPage(locator)

    result = guarded_apply_click(
        page,
        ApplyAffordance(
            selector='[data-argus-resolution-affordance="one"]',
            accessible_name="Apply now",
            role="button",
        ),
        timeout_ms=2_500,
    )

    assert result.clicked is True
    assert result.outcome == "apply_click_clicked"
    assert result.accessible_name == "Apply now"
    assert result.role == "button"
    assert page.selectors == ['[data-argus-resolution-affordance="one"]']
    assert locator.click_calls == [2_500]


def test_guarded_click_refuses_a_control_whose_role_is_submit() -> None:
    from app.services.resolution_apply_click import (
        ApplyAffordance,
        guarded_apply_click,
    )

    locator = _GuardedClickLocator(name="Apply now", role="submit")
    result = guarded_apply_click(
        _GuardedClickPage(locator),
        ApplyAffordance("#apply", "Apply now", "submit"),
        timeout_ms=2_500,
    )

    assert result.clicked is False
    assert result.outcome == "apply_click_submit_denylist_refused"
    assert locator.click_calls == []


def test_guarded_click_refuses_apply_label_on_a_submit_type_control() -> None:
    from app.services.resolution_apply_click import (
        ApplyAffordance,
        guarded_apply_click,
    )

    locator = _GuardedClickLocator(name="Apply now", control_type="submit")
    result = guarded_apply_click(
        _GuardedClickPage(locator),
        ApplyAffordance("#apply", "Apply now", "button"),
        timeout_ms=2_500,
    )

    assert result.clicked is False
    assert result.outcome == "apply_click_submit_denylist_refused"
    assert locator.click_calls == []


class _JavaScriptApplyLocator:
    def __init__(self, page: "_JavaScriptApplyPage") -> None:
        self.page = page
        self.click_calls: list[int] = []

    def count(self) -> int:
        return 1

    def evaluate(self, _script: str) -> dict[str, str]:
        return {
            "accessible_name": self.page.current_control_name,
            "role": "button",
            "type": "button",
        }

    def click(self, *, timeout: int) -> None:
        self.click_calls.append(timeout)
        self.page.url = self.page.click_destination


class _JavaScriptApplyPage:
    source_url = "http://127.0.0.1:8787/lab/resolution/js-apply"
    form_url = "http://127.0.0.1:8787/lab/resolution/js-application"

    def __init__(
        self,
        *,
        affordance_count: int = 1,
        current_control_name: str = "Apply now",
        click_destination: str | None = None,
        destination_employer: str = "Example Employer",
        destination_markup: str = "",
    ) -> None:
        self.url = self.source_url
        self.affordance_count = affordance_count
        self.current_control_name = current_control_name
        self.click_destination = click_destination or self.form_url
        self.destination_employer = destination_employer
        self.destination_markup = destination_markup
        self.locator_calls: list[str] = []
        self.wait_calls: list[tuple[str, int]] = []
        self.locator_handle = _JavaScriptApplyLocator(self)

    def content(self) -> str:
        if self.url == self.source_url:
            return (
                '<main data-source-listing><h1>Analyst</h1>'
                '<p>Example Employer</p><button type="button">Apply now</button>'
                "</main>"
            )
        if self.destination_markup:
            return self.destination_markup
        return (
            '<main data-ats="greenhouse" data-employer="'
            f'{self.destination_employer}" data-role="Analyst" '
            'data-requisition="1234" data-argus-form-identity="apply">'
            f'<form id="apply" method="post" action="{self.url}">'
            '<input name="first_name"><button type="submit">Submit application</button>'
            "</form></main>"
        )

    def evaluate(self, script: str, *_args):  # noqa: ANN001
        if "const candidates" in script:
            return {
                "href": "",
                "count": 0,
                "control_count": 0,
                "visible_root_count": 1,
                "bound_job_root_found": True,
                "page_apply_affordance_count": self.affordance_count,
                "bound_apply_affordance_count": self.affordance_count,
                "eligible_destination_count": 0,
                "candidate_urls": [],
            }
        if "data-argus-resolution-affordance" in script:
            if self.affordance_count != 1:
                return {
                    "count": self.affordance_count,
                    "page_url": self.url,
                    "candidates": ["Apply now"] * self.affordance_count,
                }
            return {
                "count": 1,
                "selector": '[data-argus-resolution-affordance="phase13"]',
                "accessible_name": "Apply now",
                "role": "button",
                "page_url": self.url,
                "candidates": ["Apply now"],
            }
        if "const root = first" in script and self.url != self.source_url:
            if self.destination_markup:
                return {}
            return {
                "provider": "greenhouse",
                "employer": self.destination_employer,
                "role": "Analyst",
                "requisition": "1234",
                "form_identity": "apply",
                "root_selector": "#apply",
                "form_action": self.url,
                "form_method": "POST",
                "control_count": 2,
                "submit_present": True,
                "form_visible": True,
                "application_entry_visible": True,
            }
        return {}

    def locator(self, selector: str) -> _JavaScriptApplyLocator:
        self.locator_calls.append(selector)
        return self.locator_handle

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.wait_calls.append((state, timeout))


def _click_capability(page: _JavaScriptApplyPage) -> SimpleNamespace:
    return SimpleNamespace(
        capability_id="phase13-capability",
        source_url=page.source_url,
        hostname="127.0.0.1",
        origin="http://127.0.0.1:8787",
        active=True,
    )


def _js_click_executor(
    page: _JavaScriptApplyPage,
    *,
    enabled: bool = True,
    budget=None,  # noqa: ANN001
):
    from app.services.navigator import _SourceResolutionExecutor
    from app.services.resolution_apply_click import ApplyClickBudget

    return _SourceResolutionExecutor(
        source_url=page.source_url,
        provider_hint="unknown",
        employer="Example Employer",
        role_title="Analyst",
        allowlist=frozenset({"127.0.0.1"}),
        apply_click_enabled=enabled,
        apply_click_budget=budget or ApplyClickBudget(25),
        apply_click_timeout_ms=2_500,
        source_capability=_click_capability(page),
        opportunity_id="opportunity-phase13",
        application_id="application-phase13",
    )


def test_feature_off_keeps_js_apply_job_detail_result_byte_identical() -> None:
    from app.services.navigator import _SourceResolutionExecutor

    default_page = _JavaScriptApplyPage()
    explicit_page = _JavaScriptApplyPage()
    default = _SourceResolutionExecutor(
        source_url=default_page.source_url,
        provider_hint="unknown",
        employer="Example Employer",
        role_title="Analyst",
        allowlist=frozenset({"127.0.0.1"}),
    )
    explicit_off = _js_click_executor(explicit_page, enabled=False)

    default.prepare(default_page)
    explicit_off.prepare(explicit_page)

    assert explicit_off.resolution == default.resolution
    assert explicit_page.locator_handle.click_calls == []


@pytest.mark.parametrize(
    "source_kind",
    [
        TargetKind.UNRESOLVED,
        TargetKind.LISTING,
        TargetKind.BLOCKED,
        TargetKind.AUTH_WALL,
        TargetKind.APPLICATION_FORM,
    ],
)
def test_click_escalation_runs_only_from_job_detail_without_get_destination(
    monkeypatch: pytest.MonkeyPatch,
    source_kind: TargetKind,
) -> None:
    import app.services.navigator as navigator_module
    from app.automation.targets import TargetResolution

    page = _JavaScriptApplyPage()
    resolution = TargetResolution(
        page.source_url,
        page.source_url,
        source_kind,
        "greenhouse" if source_kind is TargetKind.APPLICATION_FORM else "",
        source_kind is TargetKind.APPLICATION_FORM,
        source_kind is TargetKind.APPLICATION_FORM,
        ("synthetic_non_job_detail",),
        {},
    )
    monkeypatch.setattr(
        navigator_module,
        "classify_target",
        lambda *_args, **_kwargs: resolution,
    )
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is resolution
    assert page.locator_handle.click_calls == []
    assert executor._apply_click_attempted is False


def test_job_detail_js_apply_click_promotes_only_verified_application_form() -> None:
    page = _JavaScriptApplyPage()
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.APPLICATION_FORM
    assert executor.resolution.verified_for_automation is True
    assert page.locator_handle.click_calls == [2_500]
    assert page.wait_calls == [("domcontentloaded", 2_500)]
    click = executor.resolution.evidence["apply_click"]
    assert click["opportunity_id"] == "opportunity-phase13"
    assert click["page_url"] == page.source_url
    assert click["accessible_name"] == "Apply now"
    assert click["control_role"] == "button"
    assert click["result_url"] == page.form_url
    assert click["destination_kind"] == TargetKind.APPLICATION_FORM.value
    assert click["outcome"] == "verified_application_form"
    assert click["capability"] == {
        "id": "phase13-capability",
        "source_url": page.source_url,
        "hostname": "127.0.0.1",
        "origin": "http://127.0.0.1:8787",
    }


def test_two_plausible_js_apply_affordances_are_not_clicked() -> None:
    page = _JavaScriptApplyPage(affordance_count=2)
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_click_multiple_affordances" in executor.resolution.reason_codes
    assert executor.resolution.evidence["apply_click"]["outcome"] == (
        "apply_click_multiple_affordances"
    )
    assert page.locator_handle.click_calls == []


def test_apply_affordance_that_becomes_ambiguous_is_not_clicked() -> None:
    page = _JavaScriptApplyPage()
    page.locator_handle.count = lambda: 2  # type: ignore[method-assign]
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_click_ambiguous" in executor.resolution.reason_codes
    assert page.locator_handle.click_calls == []


def test_submit_denylist_refusal_stays_job_detail_without_click() -> None:
    page = _JavaScriptApplyPage(current_control_name="Submit application")
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_click_submit_denylist_refused" in executor.resolution.reason_codes
    assert executor.resolution.evidence["apply_click"]["accessible_name"] == (
        "Submit application"
    )
    assert page.locator_handle.click_calls == []


def test_exhausted_run_cap_refuses_before_resolving_the_control() -> None:
    from app.services.resolution_apply_click import ApplyClickBudget

    budget = ApplyClickBudget(1)
    assert budget.try_consume() is True
    page = _JavaScriptApplyPage()
    executor = _js_click_executor(page, budget=budget)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_click_run_cap_reached" in executor.resolution.reason_codes
    assert page.locator_calls == []


def test_post_click_employer_mismatch_is_not_promoted() -> None:
    page = _JavaScriptApplyPage(destination_employer="Different Employer")
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert executor.resolution.verified_for_automation is False
    assert "apply_destination_employer_mismatch" in executor.resolution.reason_codes
    assert page.locator_handle.click_calls == [2_500]


@pytest.mark.parametrize(
    ("destination", "markup", "expected_kind", "reason"),
    [
        (
            "http://127.0.0.1:8787/login",
            '<main><input type="password" name="password"></main>',
            TargetKind.AUTH_WALL,
            "authentication_wall",
        ),
        (
            "http://127.0.0.1:8787/challenge",
            '<main><div class="g-recaptcha" data-sitekey="synthetic"></div></main>',
            TargetKind.HUMAN_CHALLENGE,
            "human_challenge",
        ),
    ],
)
def test_post_click_human_boundaries_are_classified_without_a_click_loop(
    destination: str,
    markup: str,
    expected_kind: TargetKind,
    reason: str,
) -> None:
    page = _JavaScriptApplyPage(
        click_destination=destination,
        destination_markup=markup,
    )
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is expected_kind
    assert reason in executor.resolution.reason_codes
    assert page.locator_handle.click_calls == [2_500]
    assert executor.resolution.evidence["apply_click"]["destination_kind"] == (
        expected_kind.value
    )


def test_click_timeout_is_bounded_and_stays_job_detail() -> None:
    class ClickTimeout(TimeoutError):
        pass

    page = _JavaScriptApplyPage()

    def timeout_click(*, timeout: int) -> None:
        assert timeout == 2_500
        raise ClickTimeout("synthetic bounded timeout")

    page.locator_handle.click = timeout_click  # type: ignore[method-assign]
    executor = _js_click_executor(page)

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_click_timeout" in executor.resolution.reason_codes
    assert executor.resolution.evidence["apply_click"]["clicked"] is False


def test_click_navigation_off_capability_is_denied_without_widening() -> None:
    page = _JavaScriptApplyPage(
        click_destination="https://attacker.invalid/application"
    )
    executor = _js_click_executor(page)
    original_allowlist = executor.allowlist
    capability = executor.source_capability

    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.JOB_DETAIL
    assert "apply_click_off_capability" in executor.resolution.reason_codes
    assert executor.allowlist == original_allowlist == frozenset({"127.0.0.1"})
    assert capability.hostname == "127.0.0.1"
    assert capability.origin == "http://127.0.0.1:8787"


def test_owner_thread_egress_guard_allows_only_click_navigation_inside_capability() -> None:
    from app.automation.types import RunMode
    from app.services.navigator import HeadedSessionWorker

    class Route:
        def __init__(self, url: str) -> None:
            self.request = SimpleNamespace(
                url=url,
                method="GET",
                post_data="",
                headers={},
                resource_type="document",
                redirected_from=None,
            )
            self.continued = 0
            self.aborted: list[str] = []

        def continue_(self) -> None:
            self.continued += 1

        def abort(self, reason: str) -> None:
            self.aborted.append(reason)

    page = _JavaScriptApplyPage()
    executor = _js_click_executor(page)
    executor._apply_click_active = True
    executor._apply_click_evidence = {"outcome": "apply_click_clicked"}
    worker = HeadedSessionWorker(
        session_id="phase13-egress",
        application_id="application-phase13",
        mode=RunMode.REVIEW.value,
        url=page.source_url,
        summary={"source_resolution": True, "sterile_resolution": True},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=1,
        headless=True,
        allowlist=frozenset({"127.0.0.1"}),
        journey_executor=executor,
    )
    worker.owner_thread_id = threading.get_ident()
    capability = executor.source_capability
    original_allowlist = worker.allowlist

    allowed = Route(page.form_url)
    worker._route(allowed)
    denied = Route("https://attacker.invalid/application")
    worker._route(denied)

    assert allowed.continued == 1
    assert allowed.aborted == []
    assert denied.continued == 0
    assert denied.aborted == ["blockedbyclient"]
    assert executor._apply_click_evidence["outcome"] == "apply_click_off_capability"
    assert executor._apply_click_evidence["egress_guard"] == {
        "reached": True,
        "allowed": False,
        "fatal": True,
        "reason": "source_resolution_apply_navigation_not_bound",
    }
    assert worker.allowlist is original_allowlist
    assert capability.hostname == "127.0.0.1"
    assert capability.origin == "http://127.0.0.1:8787"


def test_resolution_click_path_cannot_reach_candidate_pii_or_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.repositories import AnswerRepository, DocumentRepository
    from app.services.documents import DocumentService
    from app.services.profile import ProfileService

    def forbidden(*_args, **_kwargs):  # noqa: ANN002, ANN003
        pytest.fail("candidate PII accessor reached from sterile resolution click")

    monkeypatch.setattr(ProfileService, "get_automation_data", forbidden)
    monkeypatch.setattr(ProfileService, "get_snapshot", forbidden)
    monkeypatch.setattr(DocumentService, "select_approved", forbidden)
    monkeypatch.setattr(DocumentRepository, "approved_by_kind", forbidden)
    monkeypatch.setattr(AnswerRepository, "by_key", forbidden)

    page = _JavaScriptApplyPage()
    executor = _js_click_executor(page)
    executor.prepare(page)

    assert executor.resolution is not None
    assert executor.resolution.kind is TargetKind.APPLICATION_FORM
    assert not hasattr(executor, "before_click")
    assert not hasattr(executor, "submission_binding")
    assert not hasattr(executor, "submit")


class _SterileLifecyclePage:
    url = "about:blank"
    frames: tuple[()] = ()

    def __init__(self) -> None:
        self.closed = False

    def on(self, *_args) -> None:  # noqa: ANN002
        return None

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    def evaluate(self, *_args):  # noqa: ANN002, ANN201
        return {"captcha": False, "reason": ""}

    def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _SterileLifecycleContext:
    def __init__(self) -> None:
        self.page = _SterileLifecyclePage()
        self.closed = False
        self.cookie_reads = 0

    def on(self, *_args) -> None:  # noqa: ANN002
        return None

    def route(self, *_args) -> None:  # noqa: ANN002
        return None

    def new_page(self) -> _SterileLifecyclePage:
        return self.page

    def cookies(self) -> list[object]:
        self.cookie_reads += 1
        return []

    def close(self) -> None:
        self.closed = True


class _SterileLifecycleBrowser:
    def __init__(self) -> None:
        self.contexts: list[_SterileLifecycleContext] = []
        self.context_options: list[dict[str, object]] = []
        self.connected = True

    def new_context(self, **kwargs) -> _SterileLifecycleContext:  # noqa: ANN003
        self.context_options.append(dict(kwargs))
        context = _SterileLifecycleContext()
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


class _SterileLifecycleRuntime:
    def __init__(self) -> None:
        self.browser = _SterileLifecycleBrowser()
        self.chromium = self
        self.stopped = False

    def launch(self, **_kwargs) -> _SterileLifecycleBrowser:
        return self.browser

    def stop(self) -> None:
        self.stopped = True


def _open_and_close_sterile_worker() -> _SterileLifecycleRuntime:
    from app.automation.types import RunMode
    from app.services.navigator import HeadedSessionWorker

    runtime = _SterileLifecycleRuntime()
    factory = SimpleNamespace(start=lambda: runtime)
    worker = HeadedSessionWorker(
        session_id="phase13-sterile",
        application_id="application-phase13",
        mode=RunMode.REVIEW.value,
        url="http://127.0.0.1:8787/lab/resolution/js-apply",
        summary={"source_resolution": True, "sterile_resolution": True},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=1,
        headless=True,
        allowlist=frozenset({"127.0.0.1"}),
        playwright_factory=lambda: factory,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._open_browser()
    worker._cleanup()
    assert worker.cleanup_complete is True
    return runtime


def test_sterile_resolution_uses_a_fresh_empty_context_destroyed_after_attempt() -> None:
    first = _open_and_close_sterile_worker()
    second = _open_and_close_sterile_worker()

    assert first.browser is not second.browser
    assert first.browser.contexts[0] is not second.browser.contexts[0]
    for runtime in (first, second):
        assert runtime.browser.context_options == [
            {
                "viewport": {"width": 1440, "height": 1000},
                "storage_state": None,
                "accept_downloads": False,
                "service_workers": "block",
            }
        ]
        assert runtime.browser.contexts[0].closed is True
        assert runtime.browser.contexts[0].cookie_reads == 1
        assert runtime.browser.contexts[0].page.closed is True
        assert runtime.browser.connected is False
        assert runtime.stopped is True


def test_source_capability_has_a_stable_audit_id_and_exact_origin_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.navigator as navigator_module

    monkeypatch.setattr(
        navigator_module,
        "safe_public_navigation_url",
        lambda _url: True,
    )
    capability = navigator_module.SourceResolutionCapability.issue(
        "https://careers.example.test/jobs/analyst"
    )

    assert len(capability.capability_id) == 32
    capability.assert_authorizes_navigation(
        "https://careers.example.test/jobs/analyst/application"
    )
    with pytest.raises(ValueError, match="exact origin"):
        capability.assert_authorizes_navigation(
            "https://apply.example.test/application"
        )
    assert capability.hostname == "careers.example.test"
    assert capability.origin == "https://careers.example.test"


class _ResolutionWorkerCapture:
    instances: list["_ResolutionWorkerCapture"] = []

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.__class__.instances.append(self)
        self.kwargs = dict(kwargs)
        self.owner_thread_id = None
        self.cleanup_complete = True
        self.cleanup_escalated = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class _QueueDrivenResolutionWorker:
    instances: list["_QueueDrivenResolutionWorker"] = []

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        from app.automation.types import SessionEvent, SessionEventType, SessionState

        self.__class__.instances.append(self)
        self.kwargs = dict(kwargs)
        self.command_queue = kwargs["command_queue"]
        self.event_queue = kwargs["event_queue"]
        self.session_id = kwargs["session_id"]
        self.owner_thread_id = None
        self.cleanup_complete = False
        self.cleanup_escalated = False
        self._alive = True
        self._terminal_events = (SessionEvent, SessionEventType, SessionState)

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        from app.automation.types import SessionCommandType

        while True:
            try:
                command = self.command_queue.get_nowait()
            except queue.Empty:
                break
            if command.command in {
                SessionCommandType.CLOSE,
                SessionCommandType.CANCEL,
                SessionCommandType.SHUTDOWN,
            }:
                event_cls, event_type, state = self._terminal_events
                self._alive = False
                self.cleanup_complete = True
                self.event_queue.put(
                    event_cls(
                        event=event_type.STATE_CHANGED,
                        session_id=self.session_id,
                        state=state.CANCELLED,
                        reason="closed",
                    )
                )
                self.event_queue.put(
                    event_cls(
                        event=event_type.CLEANUP_COMPLETE,
                        session_id=self.session_id,
                        state=state.CANCELLED,
                        payload={"owner_thread_id": 1},
                    )
                )
        return self._alive


def test_enabled_navigator_wires_review_only_sterile_executor_and_shared_budget() -> None:
    from app.automation.types import RunMode
    from app.services.navigator import (
        ApplicationNavigator,
        SourceResolutionCapability,
    )
    from app.services.resolution_apply_click import ApplyClickBudget

    source_url = "http://127.0.0.1:8787/lab/resolution/js-apply"
    capability = SourceResolutionCapability(
        source_url=source_url,
        hostname="127.0.0.1",
        origin="http://127.0.0.1:8787",
    )
    budget = ApplyClickBudget(4)
    settings = SimpleNamespace(
        live_domain_allowlist=frozenset(),
        apply_click_enabled=True,
        apply_click_timeout_ms=3_000,
        apply_click_run_cap=4,
    )
    _ResolutionWorkerCapture.instances.clear()
    navigator = ApplicationNavigator(
        settings=settings,
        worker_factory=_ResolutionWorkerCapture,
        headless=True,
        apply_click_budget=budget,
        ttl_seconds=0.01,
    )
    navigator._source_context = lambda _application_id: {
        "application_id": "application-phase13",
        "opportunity_id": "opportunity-phase13",
        "source_url": source_url,
        "employer": "Example Employer",
        "role_title": "Analyst",
        "provider_hint": "unknown",
        "cycle": "2027",
        "source": "lab",
    }

    try:
        resolution, handoff = navigator.resolve_application_target(
            "application-phase13",
            source_capability=capability,
            headed=False,
        )
    finally:
        navigator._shutdown = True

    worker = _ResolutionWorkerCapture.instances[-1]
    executor = worker.kwargs["journey_executor"]
    assert worker.kwargs["mode"] == RunMode.REVIEW.value
    assert worker.kwargs["headless"] is True
    assert worker.kwargs["summary"]["sterile_resolution"] is True
    assert executor.apply_click_enabled is True
    assert executor.apply_click_budget is budget
    assert executor.apply_click_timeout_ms == 3_000
    assert executor.source_capability is capability
    assert not hasattr(executor, "before_click")
    assert not hasattr(executor, "submission_binding")
    assert not hasattr(executor, "submit")
    assert resolution is None
    assert handoff["resumable"] is False
    assert handoff["can_continue"] is False


def test_enabled_sterile_attempt_never_reuses_an_existing_handoff_session() -> None:
    from app.services.navigator import ApplicationNavigator, SourceResolutionCapability
    from app.services.resolution_apply_click import ApplyClickBudget

    source_url = "http://127.0.0.1:8787/lab/resolution/js-apply"
    settings = SimpleNamespace(
        live_domain_allowlist=frozenset(),
        apply_click_enabled=False,
        apply_click_timeout_ms=1_000,
        apply_click_run_cap=2,
    )
    _QueueDrivenResolutionWorker.instances.clear()
    navigator = ApplicationNavigator(
        settings=settings,
        worker_factory=_QueueDrivenResolutionWorker,
        headless=True,
        ttl_seconds=0.01,
    )
    navigator._source_context = lambda _application_id: {
        "application_id": "application-phase13",
        "opportunity_id": "opportunity-phase13",
        "source_url": source_url,
        "employer": "Example Employer",
        "role_title": "Analyst",
        "provider_hint": "unknown",
        "cycle": "2027",
        "source": "lab",
    }
    first_capability = SourceResolutionCapability(
        source_url, "127.0.0.1", "http://127.0.0.1:8787"
    )
    navigator.resolve_application_target(
        "application-phase13", source_capability=first_capability, headed=False
    )
    first_worker = _QueueDrivenResolutionWorker.instances[0]
    navigator.apply_click_enabled = True
    navigator.apply_click_budget = ApplyClickBudget(2)
    second_capability = SourceResolutionCapability(
        source_url, "127.0.0.1", "http://127.0.0.1:8787"
    )

    try:
        navigator.resolve_application_target(
            "application-phase13",
            source_capability=second_capability,
            headed=False,
        )
    finally:
        navigator._shutdown = True

    assert len(_QueueDrivenResolutionWorker.instances) == 2
    assert _QueueDrivenResolutionWorker.instances[1] is not first_worker
    assert first_worker.cleanup_complete is True
    assert first_worker.is_alive() is False


def test_application_resolver_issues_and_revokes_one_capability_when_click_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.navigator as navigator_module
    from app.services.target_resolution import (
        NavigatorTargetResolver,
        ResolutionContext,
    )

    monkeypatch.setattr(
        navigator_module,
        "safe_public_navigation_url",
        lambda _url: True,
    )
    seen: list[object] = []

    class Navigator:
        settings = SimpleNamespace(apply_click_enabled=True)

        @staticmethod
        def resolve_application_target(
            application_id: str,
            *,
            source_capability,
            headed: bool,
        ):
            assert application_id == "application-phase13"
            assert headed is False
            assert source_capability.active is True
            seen.append(source_capability)
            return None, {}

    resolver = NavigatorTargetResolver(Navigator(), headed=False)
    result = resolver(
        ResolutionContext(
            opportunity_id="opportunity-phase13",
            application_id="application-phase13",
            source_url="https://careers.example.test/jobs/analyst",
            employer="Example Employer",
            role_title="Analyst",
            cycle="2027",
            provider_hint="unknown",
            source="lab",
        )
    )

    assert result == (None, {})
    assert len(seen) == 1
    assert seen[0].active is False


def test_batch_driver_shares_one_cap_across_all_row_local_navigators(
    tmp_path: Path,
) -> None:
    from app.db import Database
    from app.services.batch_target_resolution import BatchTargetResolutionDriver

    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_APPLY_CLICK": "true",
            "ARGUS_APPLY_CLICK_RUN_CAP": "2",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    try:
        driver = BatchTargetResolutionDriver(database, settings)
        first = driver.resolver_factory()
        second = driver.resolver_factory()
    finally:
        database.engine.dispose()

    assert first.apply_click_budget is second.apply_click_budget
    assert first.apply_click_budget.limit == 2


def test_batch_driver_starts_each_run_with_a_fresh_click_cap(tmp_path: Path) -> None:
    from app.db import Database
    from app.services.batch_target_resolution import (
        BatchResolveOptions,
        BatchTargetResolutionDriver,
    )

    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_APPLY_CLICK": "true",
            "ARGUS_APPLY_CLICK_RUN_CAP": "2",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    try:
        driver = BatchTargetResolutionDriver(database, settings)
        exhausted = driver.apply_click_budget
        assert exhausted is not None
        assert exhausted.try_consume() is True
        assert exhausted.try_consume() is True

        driver.run(BatchResolveOptions(dry_run=True))

        assert driver.apply_click_budget is not exhausted
        assert driver.apply_click_budget is not None
        assert driver.apply_click_budget.remaining == 2
    finally:
        database.engine.dispose()
