from __future__ import annotations

from collections.abc import Mapping as _Mapping
import queue
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.automation.host_policy import (
    FIRST_PARTY_SERVICE_MANIFEST,
    BlockedRequestImpact,
    FirstPartyServiceHost,
    VendorFirstPartyServiceRecord,
    classify_blocked_request_impact,
    load_first_party_service_manifest,
)
from app.automation.types import RunMode
from app.services.navigator import HeadedSessionWorker


_SELF_SERVICE_PATH = "/users" + "/self"


@dataclass
class _RouteFrame:
    url: str


class _RouteRequest:
    def __init__(
        self,
        *,
        url: str,
        method: str = "GET",
        resource_type: str = "fetch",
        initiator: str = "https://boards.greenhouse.io/acme/jobs/1234567",
        headers: dict[str, str] | None = None,
        post_data: str = "",
        navigation: bool = False,
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.frame = _RouteFrame(initiator)
        self.headers = headers or {}
        self.post_data = post_data
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self, request: _RouteRequest) -> None:
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self) -> None:
        self.continued += 1

    def abort(self, reason: str) -> None:
        self.aborted.append(reason)


def _navigator_worker(
    *,
    provider: str = "greenhouse",
    mode: RunMode = RunMode.PREFILL,
    enabled: bool = True,
    page_url: str = "https://boards.greenhouse.io/acme/jobs/1234567",
) -> HeadedSessionWorker:
    worker = HeadedSessionWorker(
        session_id="phase21-navigator",
        application_id="application-phase21",
        mode=mode.value,
        url=page_url,
        summary={"provider": provider},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=True,
        allowlist=frozenset(),
        egress_impact_classification_enabled=enabled,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._page = SimpleNamespace(url=page_url)
    return worker


_FIRST_PARTY_REQUEST_FIELDS = {
    "host",
    "path",
    "method",
    "resource_type",
    "manifest_vendor",
    "manifest_permission",
    "delivery",
    "carries_candidate_data",
    "candidate_data_kind",
}


@pytest.mark.parametrize(
    (
        "request_url",
        "method",
        "resource_type",
        "policy_classification",
        "expected_candidate_data",
        "expected_kind",
    ),
    [
        (
            "https://my.greenhouse.io/users/self",
            "GET",
            "fetch",
            "other",
            False,
            None,
        ),
        (
            "https://email-address-validator.us.greenhouse.io/address/validate?email=alex%40example.test",
            "HEAD",
            "fetch",
            "data_bearing",
            True,
            "email",
        ),
        (
            "https://api-geocode-earth-proxy.greenhouse.io/v1/autocomplete",
            "GET",
            "fetch",
            "data_bearing",
            True,
            "location",
        ),
        (
            "https://job-boards.cdn.greenhouse.io/assets/flags-a2kmUSbF.webp",
            "GET",
            "image",
            "passive_asset",
            False,
            None,
        ),
        # Locale JSON — versioned path, prefix-match
        (
            "https://job-boards.cdn.greenhouse.io/locales/en/job_post.a1b2c3.json",
            "GET",
            "fetch",
            "other",
            False,
            None,
        ),
        (
            "https://job-boards.cdn.greenhouse.io/locales/en/common.d4e5f6.json",
            "GET",
            "fetch",
            "other",
            False,
            None,
        ),
        # Presigned fields — authorises CV upload
        (
            "https://boards.greenhouse.io/uncacheable_attributes/presigned_fields?fields%5B%5D=resume",
            "GET",
            "fetch",
            "data_bearing",
            False,
            None,
        ),
    ],
)
def test_exact_greenhouse_service_hosts_are_nonfatal_metadata_only(
    request_url: str,
    method: str,
    resource_type: str,
    policy_classification: str,
    expected_candidate_data: bool,
    expected_kind: str | None,
) -> None:
    """Changing any literal host/type mapping must withdraw this consequence."""

    decision = classify_blocked_request_impact(
        url=request_url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=False,
        mode="prefill",
        policy_classification=policy_classification,
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert decision.impact is BlockedRequestImpact.FIRST_PARTY_SERVICE
    assert decision.reason == "first_party_service_manifest_match"
    assert decision.carries_candidate_data is expected_candidate_data
    assert decision.candidate_data_kind == expected_kind


@pytest.mark.parametrize(
    ("request_path", "sentinel"),
    [
        (
            "/address/validate/alex.path.sentinel@example.test",
            "alex.path.sentinel@example.test",
        ),
        (
            "/address/validate/alex%2Epath%2Esentinel%40example%2Etest",
            "alex%2Epath%2Esentinel%40example%2Etest",
        ),
    ],
    ids=["literal-pii-path", "percent-encoded-pii-path"],
)
def test_first_party_consequence_requires_the_exact_measured_service_path(
    request_path: str,
    sentinel: str,
) -> None:
    """A service host alone must not make a candidate-bearing path nonfatal."""

    decision = classify_blocked_request_impact(
        url=f"https://email-address-validator.us.greenhouse.io{request_path}",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode="prefill",
        policy_classification="other",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "unknown_resource_blocked"
    assert sentinel not in repr(decision)


@pytest.mark.parametrize(
    ("request_url", "resource_type"),
    [
        (
            "https://job-boards.cdn.greenhouse.io/assets/flags-a2kmUSbF.webp"
            "?email=alex%40example.test",
            "image",
        ),
    ],
)
def test_no_data_service_hosts_remain_fatal_when_policy_detects_candidate_data(
    request_url: str,
    resource_type: str,
) -> None:
    """A no-data manifest entry must not override evidence of candidate data."""

    decision = classify_blocked_request_impact(
        url=request_url,
        method="GET",
        resource_type=resource_type,
        is_navigation_request=False,
        mode="prefill",
        policy_classification="data_bearing",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/not-a-recognised-path",
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "data_bearing_request_blocked"
    assert decision.carries_candidate_data is False
    assert decision.candidate_data_kind is None


def test_self_service_endpoint_with_candidate_query_is_first_party() -> None:
    """The Greenhouse self-service endpoint has candidate_data_kind=\"\" so its known
    functional query params (e.g. job_post_id=) are recognised as non-PII
    metadata, not exfiltrated candidate data."""
    decision = classify_blocked_request_impact(
        url="https://my.greenhouse.io/users/self?email=alex%40example.test",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode="prefill",
        policy_classification="data_bearing",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/not-a-recognised-path",
    )

    assert decision.impact is BlockedRequestImpact.FIRST_PARTY_SERVICE
    assert decision.reason == "first_party_service_manifest_match"
    assert decision.carries_candidate_data is False
    assert decision.candidate_data_kind is None


def test_current_page_permission_is_exact_host_not_path_grammar() -> None:
    """Rejecting a recognised host solely for its path would exceed the policy."""

    decision = classify_blocked_request_impact(
        url="https://my.greenhouse.io/users/self",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode="prefill",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/this-is-not-a-job-url",
    )

    assert decision.impact is BlockedRequestImpact.FIRST_PARTY_SERVICE


@pytest.mark.parametrize(
    "current_page_url",
    [
        "http://boards.greenhouse.io/acme/jobs/1234567",
        "https://boards.greenhouse.io:8443/acme/jobs/1234567",
        "https://user" + "@boards.greenhouse.io/acme/jobs/1234567",
    ],
)
def test_unsafe_current_page_url_cannot_grant_manifest_permission(
    current_page_url: str,
) -> None:
    """An unsafe current-page origin must not establish same-vendor context."""

    decision = classify_blocked_request_impact(
        url="https://my.greenhouse.io/users/self",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode="prefill",
        resolved_vendor="greenhouse",
        current_page_url=current_page_url,
    )

    assert decision.impact is BlockedRequestImpact.FATAL


@pytest.mark.parametrize(
    ("request_url", "resolved_vendor", "current_page_url", "mode", "method", "resource_type", "navigation"),
    [
        (
            "https://my.greenhouse.io.attacker.example/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://not-my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            "workday",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            "greenhouse",
            "https://jobs.example.test/acme/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "review",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            None,
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "http://my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io:8443/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://user" + "@my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "POST",
            "fetch",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "xhr",
            False,
        ),
        (
            "https://my.greenhouse.io/users/self",
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "prefill",
            "GET",
            "fetch",
            True,
        ),
    ],
)
def test_non_exact_or_non_prefill_requests_remain_fatal(
    request_url: str,
    resolved_vendor: str | None,
    current_page_url: str,
    mode: str,
    method: str,
    resource_type: str,
    navigation: bool,
) -> None:
    """Any widened matching or unsafe request precondition must remain fatal."""

    decision = classify_blocked_request_impact(
        url=request_url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=navigation,
        mode=mode,
        resolved_vendor=resolved_vendor,
        current_page_url=current_page_url,
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.carries_candidate_data is False
    assert decision.candidate_data_kind is None


def test_empty_manifest_has_no_first_party_service_permission() -> None:
    """A missing manifest entry must not inherit a domain-level permission."""

    decision = classify_blocked_request_impact(
        url="https://my.greenhouse.io/users/self",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode="prefill",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
        manifest=load_first_party_service_manifest({}),
    )

    assert decision.impact is BlockedRequestImpact.FATAL


def test_loader_normalises_literals_and_returns_an_immutable_manifest() -> None:
    """Mutable or unnormalised manifest records could later widen host matching."""

    manifest = load_first_party_service_manifest(
        {
            "greenhouse": VendorFirstPartyServiceRecord(
                registrable_domain="GREENHOUSE.IO.",
                job_board_hosts=("BOARDS.GREENHOUSE.IO.",),
                service_hosts=(
                    FirstPartyServiceHost(
                        host="MY.GREENHOUSE.IO.",
                        path=_SELF_SERVICE_PATH,
                        resource_type="FETCH",
                    ),
                ),
            )
        }
    )

    assert isinstance(manifest, dict)
    assert tuple(manifest) == ("greenhouse",)
    assert manifest["greenhouse"].registrable_domain == "greenhouse.io"
    assert manifest["greenhouse"].job_board_hosts == frozenset({"boards.greenhouse.io"})
    assert (
        manifest["greenhouse"].service_hosts["my.greenhouse.io"][0].path
        == _SELF_SERVICE_PATH
    )
    assert manifest["greenhouse"].service_hosts["my.greenhouse.io"][0].resource_type == "fetch"


def test_builtin_manifest_has_only_the_measured_greenhouse_vendor_and_hosts() -> None:
    """Adding an unmeasured vendor or sixth service host must fail this contract."""

    assert set(FIRST_PARTY_SERVICE_MANIFEST) == {"greenhouse"}
    greenhouse = FIRST_PARTY_SERVICE_MANIFEST["greenhouse"]
    assert greenhouse.registrable_domain == "greenhouse.io"
    assert greenhouse.job_board_hosts == {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
    assert set(greenhouse.service_hosts) == {
        "my.greenhouse.io",
        "email-address-validator.us.greenhouse.io",
        "api-geocode-earth-proxy.greenhouse.io",
        "job-boards.cdn.greenhouse.io",
        "boards.greenhouse.io",
    }
    # service_hosts values are tuples; build a flat host→path dict for the
    # exact-match entries (exclude prefix-match endpoints).
    flat: dict[str, list[str]] = {
        host: [ep.path for ep in eps if ep.path_match == "exact"]
        for host, eps in greenhouse.service_hosts.items()
    }
    assert {
        host: paths for host, paths in flat.items() if paths
    } == {
        "my.greenhouse.io": [_SELF_SERVICE_PATH],
        "email-address-validator.us.greenhouse.io": ["/address/validate"],
        "api-geocode-earth-proxy.greenhouse.io": ["/v1/autocomplete"],
        "job-boards.cdn.greenhouse.io": ["/assets/flags-a2kmUSbF.webp"],
        "boards.greenhouse.io": ["/uncacheable_attributes/presigned_fields"],
    }


def test_manifest_match_remains_browser_blocked_but_does_not_stop_prefill() -> None:
    """Continuing the route or tripping the PREFILL probe would be unsafe."""

    worker = _navigator_worker()
    route = _Route(
        _RouteRequest(
            url=(
                "https://email-address-validator.us.greenhouse.io/address/validate"
                "?email=alex%40example.test#candidate-fragment"
            ),
            headers={"x-candidate-email": "alex@example.test"},
        )
    )
    worker._route(route)

    class _Journey:
        def __init__(self) -> None:
            self.filled: list[str] = []
            self._probe = lambda: "unexpected-unset-probe"

        def set_egress_fatal_probe(self, probe: object) -> None:
            self._probe = probe  # type: ignore[assignment]

        def set_egress_fatal_records(self, records: list[dict[str, object]]) -> None:
            pass

        def prepare(self, _page: object) -> dict[str, object]:
            for field_name in ("email", "location"):
                assert self._probe() == ""
                self.filled.append(field_name)
            return {
                "state": "NEEDS_USER",
                "reason": "Approved fields were prefilled; human review is required",
                "risk_level": 3,
                "blocked_reasons": (),
                "manifest": {"submission": "not_clicked"},
            }

    journey = _Journey()
    worker.journey_executor = journey
    worker._run_journey()

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_fatal_reason == ""
    assert journey.filled == ["email", "location"]
    record = worker._egress_records[0]
    assert record == {
        "method": "GET",
        "classification": "data_bearing",
        "origin": "https://email-address-validator.us.greenhouse.io",
        "allowed": False,
        "fatal": True,
        "record_only": False,
        "reason": "data_bearing_unapproved_origin",
        "host": "email-address-validator.us.greenhouse.io",
        "path": "/address/validate",
        "resource_type": "fetch",
        "initiator": "https://boards.greenhouse.io/acme/jobs/1234567",
        "is_navigation_request": False,
        "impact": "first_party_service",
        "impact_reason": "first_party_service_manifest_match",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": True,
        "candidate_data_kind": "email",
    }
    assert worker._journey_result["state"] == "NEEDS_USER"
    first_party = worker._journey_result["first_party_requests"]
    assert first_party == worker._journey_result["manifest"]["first_party_requests"]
    assert set(first_party[0]) == _FIRST_PARTY_REQUEST_FIELDS
    assert first_party[0]["manifest_vendor"] == "greenhouse"
    assert first_party[0]["manifest_permission"] is True
    assert first_party[0]["delivery"] == "blocked"
    assert "allowed" not in first_party[0]


@pytest.mark.parametrize(
    ("provider", "page_url", "request_url", "mode"),
    [
        (
            "workday",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "https://my.greenhouse.io/users/self",
            RunMode.PREFILL,
        ),
        (
            "greenhouse",
            "https://jobs.example.test/acme/1234567",
            "https://my.greenhouse.io/users/self",
            RunMode.PREFILL,
        ),
        (
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "https://my.greenhouse.io.attacker.example/users/self",
            RunMode.PREFILL,
        ),
        (
            "greenhouse",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            "https://my.greenhouse.io/validate",
            RunMode.REVIEW,
        ),
    ],
)
def test_non_manifest_navigator_routes_remain_fatal(
    provider: str,
    page_url: str,
    request_url: str,
    mode: RunMode,
) -> None:
    """Wrong vendor, page, lookalike, or mode must retain fatal handling."""

    worker = _navigator_worker(
        provider=provider,
        page_url=page_url,
        mode=mode,
    )
    route = _Route(_RouteRequest(url=request_url))
    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[0]["impact"] == "fatal"
    assert worker._egress_fatal_reason


def test_manifest_permission_uses_the_current_page_not_the_start_url() -> None:
    """Using the stale start URL could grant a departed page a permission."""

    worker = _navigator_worker()
    worker._page = SimpleNamespace(url="https://jobs.example.test/not-greenhouse")
    route = _Route(
        _RouteRequest(url="https://my.greenhouse.io/users/self")
    )
    worker._route(route)

    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[0]["impact"] == "fatal"
    assert worker._egress_records[0]["manifest_permission"] is False


def test_first_party_evidence_is_private_and_keeps_truthful_blocked_subset() -> None:
    """A raw URL/header/value or a missing blocked fact would leak or mislead."""

    worker = _navigator_worker()
    worker._route(
        _Route(
            _RouteRequest(
                url=(
                    "https://email-address-validator.us.greenhouse.io/address/validate"
                    "?email=alex%40example.test#candidate-fragment"
                ),
                headers={"x-candidate-email": "alex@example.test"},
            )
        )
    )
    worker._route(
        _Route(
            _RouteRequest(
                url=(
                    "https://api-geocode-earth-proxy.greenhouse.io/v1/autocomplete"
                    "?location=221B%20Baker%20Street#candidate-fragment"
                ),
                headers={"x-candidate-location": "221B Baker Street"},
            )
        )
    )
    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {"submission": "not_clicked"},
        }
    )

    result = worker._journey_result
    first_party = result["first_party_requests"]
    assert [item["candidate_data_kind"] for item in first_party] == ["email", "location"]
    assert all(item["carries_candidate_data"] is True for item in first_party)
    assert all(item["delivery"] == "blocked" for item in first_party)
    assert result["manifest"]["blocked_resources"] == result["blocked_requests"]
    assert result["manifest"]["first_party_requests"] == first_party
    assert all(set(item) == _FIRST_PARTY_REQUEST_FIELDS for item in first_party)
    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    evidence = repr((worker._egress_records, events, result))
    for secret in (
        "alex@example.test",
        "221B Baker Street",
        "candidate-fragment",
        "x-candidate-email",
        "x-candidate-location",
        "?email=",
        "?location=",
    ):
        assert secret not in evidence


@pytest.mark.parametrize(
    ("request_path", "sentinel"),
    [
        (
            "/address/validate/alex.navigator.path@example.test",
            "alex.navigator.path@example.test",
        ),
        (
            "/address/validate/alex%2Enavigator%2Epath%40example%2Etest",
            "alex%2Enavigator%2Epath%40example%2Etest",
        ),
    ],
    ids=["literal-pii-path", "percent-encoded-pii-path"],
)
def test_noncanonical_service_paths_never_cross_enabled_prefill_evidence_boundaries(
    request_path: str,
    sentinel: str,
) -> None:
    """An unsafe route path must not reach REQUEST, handoff, or final-manifest evidence."""

    worker = _navigator_worker()
    route = _Route(
        _RouteRequest(
            url=f"https://email-address-validator.us.greenhouse.io{request_path}",
        )
    )

    worker._route(route)
    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {"submission": "not_clicked"},
        }
    )

    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    request_events = [event for event in events if event.event.value == "REQUEST"]
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[0]["impact"] == "fatal"
    assert worker._egress_records[0]["path"] == ""
    assert request_events[0].payload["egress"]["path"] == ""
    assert worker._journey_result["blocked_requests"][0]["path"] == ""
    assert worker._journey_result["manifest"]["blocked_resources"][0]["path"] == ""
    assert worker._journey_result["first_party_requests"] == []
    assert worker._journey_result["manifest"]["first_party_requests"] == []
    assert sentinel not in repr((worker._egress_records, events, worker._journey_result))


@pytest.mark.parametrize(
    "mode",
    [RunMode.REVIEW, RunMode.SUBMIT],
    ids=["review", "submit"],
)
def test_non_prefill_request_events_preserve_legacy_query_free_service_paths(
    mode: RunMode,
) -> None:
    """Route-time PREFILL privacy must not rewrite REVIEW or SUBMIT evidence."""

    request_path = "/address/validate/alex.legacy.path@example.test"
    worker = _navigator_worker(mode=mode, enabled=True)
    route = _Route(
        _RouteRequest(
            url=(
                "https://email-address-validator.us.greenhouse.io"
                f"{request_path}?address=alex%40example.test#legacy-fragment"
            ),
        )
    )

    worker._route(route)

    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    request_events = [event for event in events if event.event.value == "REQUEST"]
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[0]["impact"] == "fatal"
    assert worker._egress_records[0]["path"] == request_path
    assert request_events[0].payload["egress"]["path"] == request_path
    assert "?address=" not in repr((worker._egress_records, request_events))
    assert "legacy-fragment" not in repr((worker._egress_records, request_events))


def test_final_manifest_reapplies_the_private_first_party_whitelist() -> None:
    """Copying supplied request maps into the final manifest would leak values."""

    worker = _navigator_worker()
    manifest = worker._authoritative_manifest(
        {
            "blocked_resources": [
                {
                    "url": "https://my.greenhouse.io/validate?email=alex%40example.test",
                    "headers": {"x-candidate-email": "alex@example.test"},
                    "host": "my.greenhouse.io",
                    "path": "/validate?email=alex%40example.test",
                    "reason": "sentinel-free-text",
                }
            ],
            "first_party_requests": [
                {
                    "url": "https://my.greenhouse.io/validate?email=alex%40example.test",
                    "headers": {"x-candidate-email": "alex@example.test"},
                    "host": "my.greenhouse.io",
                    "path": "/validate?email=alex%40example.test",
                    "method": "GET",
                    "resource_type": "fetch",
                    "initiator": "https://boards.greenhouse.io/acme/jobs/1234567?location=221B",
                    "impact": "first_party_service",
                    "impact_reason": "first_party_service_manifest_match",
                    "classification": "data_bearing",
                    "reason": "sentinel-free-text",
                    "manifest_vendor": "greenhouse",
                    "manifest_permission": True,
                    "delivery": "blocked",
                    "carries_candidate_data": True,
                    "candidate_data_kind": "email",
                }
            ]
        }
    )

    evidence = repr(manifest)
    assert "alex@example.test" not in evidence
    assert "?email=" not in evidence
    assert "?location=" not in evidence
    assert "headers" not in evidence
    assert manifest["first_party_requests"] == []
    assert manifest["blocked_resources"] == []


def test_first_party_projection_requires_actual_booleans() -> None:
    """String booleans must not turn a denied permission into a true marker."""

    evidence = HeadedSessionWorker._handoff_blocked_request(
        {
            "host": "my.greenhouse.io",
            "path": "/validate",
            "manifest_permission": "false",
            "carries_candidate_data": "false",
            "candidate_data_kind": "email",
        }
    )

    assert evidence["manifest_permission"] is False
    assert evidence["carries_candidate_data"] is False


def test_first_party_lists_are_rederived_empty_when_owner_has_no_matches() -> None:
    """Stale journey or manifest first-party lists must never survive a rerun."""

    worker = _navigator_worker()
    result = worker._reconcile_prefill_egress(
        {
            "state": "FINAL_REVIEW",
            "first_party_requests": [{"sentinel": "stale-top-level"}],
            "manifest": {"first_party_requests": [{"sentinel": "stale-manifest"}]},
        }
    )

    assert result["first_party_requests"] == []
    assert result["manifest"]["first_party_requests"] == []


def test_first_party_projection_preserves_every_current_match() -> None:
    """A hidden 200-record slice would silently drop blocked-request evidence."""

    worker = _navigator_worker()
    worker._egress_records = [
        {
            "allowed": False,
            "host": "my.greenhouse.io",
            "path": _SELF_SERVICE_PATH,
            "method": "GET",
            "resource_type": "fetch",
            "impact": "first_party_service",
            "manifest_vendor": "greenhouse",
            "manifest_permission": True,
            "delivery": "blocked",
            "carries_candidate_data": False,
            "candidate_data_kind": "",
        }
        for index in range(201)
    ]

    result = worker._reconcile_prefill_egress({"state": "FINAL_REVIEW", "manifest": {}})

    assert len(result["first_party_requests"]) == 201
    assert len(result["manifest"]["first_party_requests"]) == 201


def test_enabled_missing_url_evidence_has_no_url_key() -> None:
    """An opaque request must not retain an empty raw-URL field in enabled mode."""

    worker = _navigator_worker()
    worker._route(_Route(_RouteRequest(url="")))

    assert "url" not in worker._egress_records[0]


def test_submit_result_is_not_rewritten_by_first_party_reconciliation() -> None:
    """Applying this PREFILL-only consequence policy in SUBMIT would be unsafe."""

    worker = _navigator_worker(mode=RunMode.SUBMIT)
    original = {
        "state": "ACTIVE",
        "reason": "Existing submit result",
        "risk_level": 4,
        "blocked_reasons": ("existing_guard",),
        "manifest": {"submission": "not_clicked"},
    }

    worker._emit_journey_result(original)

    assert worker._journey_result == original


def test_submit_final_manifest_does_not_add_prefill_egress_evidence() -> None:
    """Adding Phase 21 fields to SUBMIT final review would widen its scope."""

    worker = _navigator_worker(mode=RunMode.SUBMIT)
    manifest = worker._authoritative_manifest({"submission": "not_clicked"})

    assert "blocked_resources" not in manifest
    assert "first_party_requests" not in manifest


@pytest.mark.parametrize(
    ("mode", "enabled"),
    [
        (RunMode.PREFILL, False),
        (RunMode.REVIEW, True),
        (RunMode.SUBMIT, True),
    ],
)
def test_non_phase21_scopes_preserve_sanitised_legacy_blocked_resources(
    mode: RunMode,
    enabled: bool,
) -> None:
    """Dropping legacy evidence or copying raw supplied values would regress audit truth."""

    worker = _navigator_worker(mode=mode, enabled=enabled)
    manifest = worker._authoritative_manifest(
        {
            "blocked_resources": [
                {
                    "url": "https://my.greenhouse.io/validate?email=alex%40example.test",
                    "headers": {"x-candidate-email": "alex@example.test"},
                    "host": "my.greenhouse.io",
                    "path": "/validate?email=alex%40example.test",
                    "method": "GET",
                    "resource_type": "fetch",
                    "initiator": "https://boards.greenhouse.io/acme/jobs/1234567?location=221B",
                    "classification": "data_bearing",
                    "reason": "legacy_blocked_resource",
                    "fatal": True,
                    "record_only": False,
                    "impact": "fatal",
                    "impact_reason": "legacy_impact",
                }
            ]
        }
    )

    assert "first_party_requests" not in manifest
    blocked = manifest["blocked_resources"]
    assert len(blocked) == 1
    assert blocked[0]["host"] == "my.greenhouse.io"
    assert blocked[0]["path"] == "/validate"
    assert blocked[0]["initiator"] == "https://boards.greenhouse.io/acme/jobs/1234567"
    assert blocked[0]["reason"] == "legacy_blocked_resource"
    evidence = repr(blocked)
    for secret in ("alex@example.test", "?email=", "?location=", "headers"):
        assert secret not in evidence


def test_disabled_first_party_mode_preserves_phase17_route_record_shape() -> None:
    """Adding private-evidence fields while disabled would break the rollback."""

    worker = _navigator_worker(enabled=False)
    request_url = "https://my.greenhouse.io/validate?email=alex%40example.test"
    route = _Route(_RouteRequest(url=request_url))
    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records == [
        {
            "url": request_url,
            "method": "GET",
            "classification": "data_bearing",
            "origin": "https://my.greenhouse.io",
            "allowed": False,
            "fatal": True,
            "record_only": False,
            "reason": "data_bearing_unapproved_origin",
        }
    ]


@pytest.mark.parametrize(
    "record",
    [
        VendorFirstPartyServiceRecord(
            registrable_domain="attacker.example",
            job_board_hosts=("boards.attacker.example",),
            service_hosts=(
                FirstPartyServiceHost(
                    "my.attacker.example",
                    _SELF_SERVICE_PATH,
                    "fetch",
                ),
            ),
        ),
        VendorFirstPartyServiceRecord(
            registrable_domain="co.uk",
            job_board_hosts=("boards.co.uk",),
            service_hosts=(
                FirstPartyServiceHost("my.co.uk", _SELF_SERVICE_PATH, "fetch"),
            ),
        ),
        VendorFirstPartyServiceRecord(
            registrable_domain="127.0.0.1",
            job_board_hosts=("127.0.0.1",),
            service_hosts=(
                FirstPartyServiceHost("127.0.0.1", _SELF_SERVICE_PATH, "fetch"),
            ),
        ),
    ],
)
def test_known_vendor_cannot_redefine_its_authoritative_registrable_domain(
    record: VendorFirstPartyServiceRecord,
) -> None:
    """A self-declared Greenhouse domain must not create a new permission space."""

    with pytest.raises(ValueError):
        load_first_party_service_manifest({"greenhouse": record})


@pytest.mark.parametrize(
    "record",
    [
        VendorFirstPartyServiceRecord(
            registrable_domain="greenhouse.io",
            job_board_hosts=("boards.greenhouse.io",),
            service_hosts=(
                FirstPartyServiceHost(
                    "api.attacker.example",
                    _SELF_SERVICE_PATH,
                    "fetch",
                ),
            ),
        ),
        VendorFirstPartyServiceRecord(
            registrable_domain="greenhouse.io",
            job_board_hosts=("boards.greenhouse.io",),
            service_hosts=(
                FirstPartyServiceHost(
                    "my.greenhouse.io",
                    _SELF_SERVICE_PATH,
                    "fetch",
                ),
                FirstPartyServiceHost("MY.GREENHOUSE.IO.", _SELF_SERVICE_PATH, "fetch"),
            ),
        ),
    ],
)
def test_loader_rejects_cross_domain_and_duplicate_literal_hosts(
    record: VendorFirstPartyServiceRecord,
) -> None:
    """Cross-domain or duplicate records must fail while the manifest is loaded."""

    with pytest.raises(ValueError):
        load_first_party_service_manifest({"greenhouse": record})
