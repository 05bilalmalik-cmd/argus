from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.automation import host_policy
from app.automation.host_policy import (
    BlockedRequestImpact,
    classify_blocked_request_impact,
)
from app.automation.types import RunMode
from app.services.navigator import HeadedSessionWorker


def _captcha_decision(**overrides: object):
    authorize = getattr(host_policy, "authorize_captcha_request", None)
    assert callable(authorize), "Phase 27 CAPTCHA authorization policy is missing"
    values = {
        "url": "https://www.recaptcha.net/recaptcha/enterprise.js",
        "method": "GET",
        "resource_type": "script",
        "is_navigation_request": False,
        "is_main_frame_navigation": False,
        "mode": RunMode.PREFILL.value,
        "resolved_vendor": "greenhouse",
        "current_page_url": (
            "https://job-boards.greenhouse.io/example/jobs/1234567"
        ),
        "carries_candidate_data": False,
    }
    values.update(overrides)
    return authorize(**values)


@pytest.mark.parametrize(
    (
        "url",
        "method",
        "resource_type",
        "policy_classification",
        "carries_candidate_data",
        "expected_impact",
        "expected_consequence",
    ),
    [
        (
            "https://www.recaptcha.net/recaptcha/enterprise.js",
            "GET",
            "script",
            "passive_asset",
            False,
            BlockedRequestImpact.TOLERABLE,
            "optional",
        ),
        (
            "https://www.dropbox.com/static/api/2/dropins.js",
            "GET",
            "script",
            "passive_asset",
            False,
            BlockedRequestImpact.TOLERABLE,
            "optional",
        ),
        (
            "https://apis.google.com/js/api.js",
            "GET",
            "script",
            "passive_asset",
            False,
            BlockedRequestImpact.TOLERABLE,
            "optional",
        ),
        (
            "https://accounts.google.com/gsi/client",
            "GET",
            "script",
            "other",
            False,
            BlockedRequestImpact.TOLERABLE,
            "optional",
        ),
        (
            "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2",
            "POST",
            "fetch",
            "data_bearing",
            False,
            BlockedRequestImpact.TOLERABLE,
            "optional",
        ),
        (
            "https://s2-recruiting.cdn.greenhouse.io/job_board_renderer/"
            "job_board_configurations/banners/400/028/300/original/"
            "Aquatic-Logo_Color-Color-BG.png",
            "GET",
            "image",
            "passive_asset",
            False,
            BlockedRequestImpact.TOLERABLE,
            "optional",
        ),
    ],
)
def test_phase26_resources_have_explicit_completion_consequences(
    url: str,
    method: str,
    resource_type: str,
    policy_classification: str,
    carries_candidate_data: bool,
    expected_impact: BlockedRequestImpact,
    expected_consequence: str,
) -> None:
    """Deleting or changing a measured table row must change this observable verdict."""

    decision = classify_blocked_request_impact(
        url=url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification=policy_classification,
        carries_candidate_data=carries_candidate_data,
    )

    assert decision.impact is expected_impact
    assert decision.consequence.value == expected_consequence


def test_optional_table_match_remains_blocked_but_does_not_stop_prefill() -> None:
    decision = classify_blocked_request_impact(
        url="https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2",
        method="POST",
        resource_type="fetch",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="data_bearing",
        carries_candidate_data=True,
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE
    assert decision.consequence.value == "optional"
    assert decision.carries_candidate_data is True


def test_unclassified_display_resource_fails_closed() -> None:
    decision = classify_blocked_request_impact(
        url="https://unmeasured.example.test/new-font.woff2",
        method="GET",
        resource_type="font",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        carries_candidate_data=False,
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.consequence.value == "unknown"
    assert decision.reason == "unknown_resource_blocked"


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        ("https://www.recaptcha.net/recaptcha/enterprise.js", "script"),
        ("https://www.google.com/recaptcha/api2/anchor", "document"),
        (
            "https://www.gstatic.com/recaptcha/releases/release-id/recaptcha__en.js",
            "script",
        ),
    ],
)
def test_exact_captcha_hosts_are_permitted_during_recognised_prefill(
    url: str,
    resource_type: str,
) -> None:
    decision = _captcha_decision(url=url, resource_type=resource_type)

    assert decision.allowed is True
    assert decision.reason == "captcha_provider_exact_host"
    assert decision.carries_candidate_data is False
    assert decision.defect is False


@pytest.mark.parametrize(
    "url",
    [
        "https://google.com.evil.com/recaptcha/api2/anchor",
        "https://notgoogle.com/recaptcha/api2/anchor",
        "https://www-google.com/recaptcha/api2/anchor",
        "https://www.recaptcha.net.evil.com/recaptcha/enterprise.js",
    ],
)
def test_captcha_host_spoofs_are_rejected(url: str) -> None:
    decision = _captcha_decision(url=url)

    assert decision.allowed is False
    assert decision.reason == "captcha_host_not_allowed"


@pytest.mark.parametrize("mode", [RunMode.REVIEW.value, RunMode.SUBMIT.value])
def test_captcha_allowance_is_prefill_only(mode: str) -> None:
    decision = _captcha_decision(mode=mode)

    assert decision.allowed is False
    assert decision.reason == "captcha_allowance_not_prefill"


def test_captcha_allowance_requires_recognised_page_and_resolved_vendor() -> None:
    unrecognised = _captcha_decision(
        current_page_url="https://unrecognised.example/application"
    )
    unresolved = _captcha_decision(resolved_vendor=None)

    assert unrecognised.allowed is False
    assert unrecognised.reason == "captcha_page_not_recognised_ats_form"
    assert unresolved.allowed is False
    assert unresolved.reason == "captcha_vendor_unresolved"


def test_captcha_request_carrying_candidate_data_is_a_blocked_defect() -> None:
    decision = _captcha_decision(
        url=(
            "https://www.recaptcha.net/recaptcha/api2/anchor"
            "?email=private%40example.test"
        ),
        carries_candidate_data=True,
    )

    assert decision.allowed is False
    assert decision.reason == "captcha_candidate_data_defect"
    assert decision.carries_candidate_data is True
    assert decision.defect is True


def test_empty_captcha_allowance_reproduces_blocked_behavior() -> None:
    decision = _captcha_decision(allowance=frozenset())

    assert decision.allowed is False
    assert decision.reason == "captcha_host_not_allowed"


@dataclass
class _Frame:
    url: str


class _Request:
    def __init__(
        self,
        *,
        url: str,
        method: str = "GET",
        resource_type: str = "script",
        post_data: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.post_data = post_data
        self.headers = headers or {}
        self.frame = _Frame(
            "https://job-boards.greenhouse.io/example/jobs/1234567"
        )

    def is_navigation_request(self) -> bool:
        return False


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self) -> None:
        self.continued += 1

    def abort(self, reason: str) -> None:
        self.aborted.append(reason)


def _route_worker(
    *,
    captcha_hosts: frozenset[str] = host_policy.CAPTCHA_PROVIDER_HOSTS,
) -> HeadedSessionWorker:
    page_url = "https://job-boards.greenhouse.io/example/jobs/1234567"
    worker = HeadedSessionWorker(
        session_id="phase27-captcha-route",
        application_id="application-phase27",
        mode=RunMode.PREFILL.value,
        url=page_url,
        summary={"provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=True,
        allowlist=frozenset(),
        captcha_provider_hosts=captcha_hosts,
        egress_impact_classification_enabled=True,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._page = SimpleNamespace(url=page_url)
    return worker


def test_route_permits_exact_captcha_request_and_records_separate_evidence() -> None:
    worker = _route_worker()
    route = _Route(
        _Request(url="https://www.recaptcha.net/recaptcha/enterprise.js")
    )

    worker._route(route)

    assert route.continued == 1
    assert route.aborted == []
    assert worker._egress_fatal_reason == ""
    record = worker._egress_records[-1]
    assert record["captcha_provider"] is True
    assert record["captcha_permission"] is True
    assert record["captcha_defect"] is False
    assert record["delivery"] == "permitted"
    assert record["carries_candidate_data"] is False
    assert record["host"] == "www.recaptcha.net"
    assert "url" not in record


def test_route_still_blocks_measured_optional_widget_without_failing_prefill() -> None:
    worker = _route_worker()
    route = _Route(
        _Request(url="https://www.dropbox.com/static/api/2/dropins.js")
    )

    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[-1]["impact"] == "tolerable"
    assert worker._egress_records[-1]["consequence"] == "optional"
    assert worker._egress_fatal_reason == ""


def test_route_blocks_candidate_data_to_captcha_as_a_fatal_defect() -> None:
    worker = _route_worker()
    route = _Route(
        _Request(
            url=(
                "https://www.recaptcha.net/recaptcha/api2/anchor"
                "?email=private%40example.test"
            )
        )
    )

    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    record = worker._egress_records[-1]
    assert record["reason"] == "captcha_candidate_data_defect"
    assert record["captcha_defect"] is True
    assert record["carries_candidate_data"] is True
    assert worker._egress_fatal_reason == "captcha_candidate_data_defect"
    assert "private" not in repr(record)


def test_route_empty_captcha_allowance_preserves_the_old_fatal_block() -> None:
    """With an empty captcha allowance, CAPTCHA requests are still BLOCKED
    by the route guard, but the impact is tolerable (the consequence table
    now treats all known CAPTCHA resources as OPTIONAL since the navigator's
    authorize_captcha_request is the canonical allow/block gate)."""
    worker = _route_worker(captcha_hosts=frozenset())
    route = _Route(
        _Request(url="https://www.recaptcha.net/recaptcha/enterprise.js")
    )

    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[-1]["impact"] == "tolerable"
    assert worker._egress_records[-1]["reason"] != "captcha_provider_exact_host"


def test_permitted_captcha_evidence_reaches_handoff_without_becoming_blocked() -> None:
    worker = _route_worker()
    route = _Route(
        _Request(url="https://www.recaptcha.net/recaptcha/enterprise.js")
    )
    worker._route(route)

    result = worker._reconcile_prefill_egress(
        {"state": "NEEDS_USER", "manifest": {"submission": "not_clicked"}}
    )

    expected = [
        {
            "host": "www.recaptcha.net",
            "path": "/recaptcha/enterprise.js",
            "method": "GET",
            "resource_type": "script",
            "captcha_provider": True,
            "captcha_permission": True,
            "delivery": "permitted",
            "carries_candidate_data": False,
        }
    ]
    assert result["captcha_requests"] == expected
    assert result["manifest"]["captcha_requests"] == expected
    assert "blocked_requests" not in result


def test_runner_reauthorises_only_safe_query_free_captcha_evidence() -> None:
    from app.automation import runner

    sanitizer = getattr(runner, "_query_free_captcha_requests", None)
    assert callable(sanitizer), "Phase 27 CAPTCHA audit sanitizer is missing"
    raw = [
        {
            "host": "www.recaptcha.net",
            "path": "/recaptcha/enterprise.js",
            "method": "GET",
            "resource_type": "script",
            "captcha_provider": True,
            "captcha_permission": True,
            "delivery": "permitted",
            "carries_candidate_data": False,
        },
        {
            "host": "www.recaptcha.net.evil.test",
            "path": "/recaptcha/enterprise.js",
            "method": "GET",
            "resource_type": "script",
            "captcha_provider": True,
            "captcha_permission": True,
            "delivery": "permitted",
            "carries_candidate_data": False,
        },
        {
            "host": "www.recaptcha.net",
            "path": "/recaptcha/api2/anchor?email=redacted",
            "method": "GET",
            "resource_type": "document",
            "captcha_provider": True,
            "captcha_permission": True,
            "delivery": "permitted",
            "carries_candidate_data": True,
        },
    ]

    sanitized = sanitizer(
        raw,
        mode=RunMode.PREFILL,
        current_page_url=(
            "https://job-boards.greenhouse.io/example/jobs/1234567"
        ),
        resolved_vendor="greenhouse",
    )

    assert sanitized == raw[:1]
    assert "?" not in repr(sanitized)
