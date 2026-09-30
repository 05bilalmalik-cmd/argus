# TDD: R3 — INSPECT/PREFILL/SUBMIT mode separation + R11 egress manifests.
# Generic-adapter results on non-lab origins are review-only; candidate
# data may never leak to an unapproved destination.
from __future__ import annotations

import pytest


def test_run_mode_enum_has_inspect_prefill_submit():
    from app.automation.types import RunMode

    assert RunMode.INSPECT.value == "inspect"
    assert RunMode.PREFILL.value == "prefill"
    assert RunMode.SUBMIT.value == "submit"


def test_egress_manifest_classifies_passive_assets():
    from app.automation.host_policy import classify_request

    passive = [
        ("https://cdn.example.com/logo.png", "GET", ""),
        ("https://fonts.example.com/f.woff2", "GET", ""),
        ("https://portal.example.com/styles.css", "GET", ""),
        ("https://portal.example.com/app.js", "GET", ""),
        ("https://portal.example.com/favicon.ico", "GET", ""),
    ]
    for url, method, payload in passive:
        verdict = classify_request(url, method, payload)
        assert verdict == "passive_asset", url


def test_candidate_bearing_post_is_data_bearing():
    from app.automation.host_policy import classify_request

    assert (
        classify_request(
            "https://portal.example.com/apply/submit", "POST", "first_name=x"
        )
        == "data_bearing"
    )
    assert (
        classify_request("https://portal.example.com/api/answers", "PUT", "{}")
        == "data_bearing"
    )


def test_unknown_data_bearing_to_unapproved_host_must_block():
    from app.automation.host_policy import egress_allowed

    # Unapproved destination with candidate data: block, always.
    assert (
        egress_allowed(
            "https://collector.attacker.example/collect",
            "POST",
            "email=demo.candidate@example.test",
            approved_hosts=frozenset({"portal.example.com"}),
        )
        is False
    )


def test_passive_unknown_assets_are_blockable_without_failing_inspection():
    from app.automation.host_policy import is_passive_asset

    # A blocked analytics script must not abort a safe inspection as long as
    # it carries no candidate data.
    assert is_passive_asset("https://analytics.thirdparty.example/track.js")


def test_mutating_methods_are_data_bearing_even_without_a_body():
    from app.automation.host_policy import classify_request

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert classify_request("https://portal.example.com/apply", method, "") == "data_bearing"


def test_candidate_data_in_get_query_or_headers_is_data_bearing():
    from app.automation.host_policy import classify_request

    assert (
        classify_request(
            "https://cdn.example.com/app.js?email=demo%40example.test",
            "GET",
            "",
        )
        == "data_bearing"
    )
    assert (
        classify_request(
            "https://cdn.example.com/app.js",
            "GET",
            "",
            headers={"X-Candidate-Phone": "+44 7700 900123"},
        )
        == "data_bearing"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/point72/jobs/8423978002?gh_jid=8423978002",
        "https://job-boards.greenhouse.io/point72/jobs/8423978002?gh_jid=8423978002",
        "https://job-boards.eu.greenhouse.io/isam/jobs/4949792101?gh_jid=4949792101",
    ],
)
def test_greenhouse_job_identity_query_is_safe_read_only_navigation(url: str):
    """A path-bound Greenhouse job id must not be mistaken for a phone."""

    from app.automation.host_policy import classify_egress, classify_request

    assert classify_request(url, "GET", "") == "other"
    decision = classify_egress(
        url,
        "GET",
        "",
        approved_hosts={
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "job-boards.eu.greenhouse.io",
        },
    )
    assert decision.allowed is True
    assert decision.fatal is False


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/point72/jobs/8423978002?gh_jid=447700900123",
        "https://boards.greenhouse.io/point72/jobs/9999999999?gh_jid=8423978002",
        "https://portal.example.com/point72/jobs/8423978002?gh_jid=8423978002",
    ],
)
def test_greenhouse_job_identity_exception_fails_closed_without_exact_binding(url: str):
    """Host, path id, and query id must all agree before numeric data is safe."""

    from app.automation.host_policy import classify_request

    assert classify_request(url, "GET", "") == "data_bearing"


def test_origins_are_normalized_and_reject_paths_credentials_and_non_http():
    from app.automation.host_policy import normalize_origin, validate_origin

    assert normalize_origin("HTTPS://Portal.Example.com:443/") == "https://portal.example.com"
    assert normalize_origin("http://Portal.Example.com:80") == "http://portal.example.com"
    for raw in (
        "https://portal.example.com/apply",
        "https://user:password@portal.example.com",
        "ftp://portal.example.com",
        "https://",
        "https://portal.example.com/%2e%2e",
    ):
        try:
            validate_origin(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"origin unexpectedly accepted: {raw}")


def test_unapproved_passive_get_is_recordable_without_being_allowed():
    from app.automation.host_policy import classify_egress, egress_allowed

    decision = classify_egress(
        "https://analytics.thirdparty.example/track.js",
        "GET",
        "",
        approved_hosts={"portal.example.com"},
    )
    assert decision.classification == "passive_asset"
    assert decision.allowed is False
    assert decision.fatal is False
    assert egress_allowed(
        "https://analytics.thirdparty.example/track.js",
        "GET",
        "",
        approved_hosts={"portal.example.com"},
    ) is False


def test_unknown_or_empty_method_fails_closed_even_on_an_approved_host():
    from app.automation.host_policy import classify_request, egress_allowed

    for method in (None, "", "TRACE", "not-a-method"):
        assert classify_request("https://portal.example.com/apply", method, "") == "data_bearing"
        assert egress_allowed(
            "https://portal.example.com/apply",
            method,
            "",
            approved_hosts={"portal.example.com"},
        ) is False


def test_recursive_percent_decoding_and_encoded_json_are_candidate_data():
    import base64

    from app.automation.host_policy import classify_request

    triple_encoded_email = "demo%2540example%252Etest"
    assert (
        classify_request(
            f"https://cdn.example.com/app.js?payload={triple_encoded_email}",
            "GET",
            "",
        )
        == "data_bearing"
    )
    encoded_json = base64.urlsafe_b64encode(
        b'{"email":"demo.candidate@example.test","phone":"+447700900123"}'
    ).decode()
    assert (
        classify_request(
            f"https://cdn.example.com/app.js?state={encoded_json}",
            "GET",
            "",
            headers={"X-State": encoded_json},
        )
        == "data_bearing"
    )


def test_data_bearing_approval_requires_exact_canonical_origin_and_effective_port():
    from app.automation.host_policy import egress_allowed

    assert (
        egress_allowed(
            "https://portal.example.com:8443/apply",
            "POST",
            "email=x@example.test",
            approved_hosts={"portal.example.com"},
        )
        is False
    )
    assert (
        egress_allowed(
            "https://portal.example.com:8443/apply",
            "POST",
            "email=x@example.test",
            approved_hosts={"https://portal.example.com:8443"},
        )
        is True
    )


def test_origin_validation_rejects_unicode_and_empty_port():
    from app.automation.host_policy import validate_origin

    for raw in (None, "https://portal.example.com:", "https://éxample.com"):
        try:
            validate_origin(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid origin accepted: {raw!r}")


def test_nested_base64_layers_in_query_and_headers_are_candidate_data():
    import base64
    from urllib.parse import quote

    from app.automation.host_policy import classify_request

    encoded = b'{"candidate":{"email":"demo.candidate@example.test"}}'
    for _ in range(3):
        encoded = base64.urlsafe_b64encode(encoded)
    token = quote(encoded.decode("ascii"), safe="")
    assert classify_request(
        f"https://cdn.example.com/app.js?state={token}",
        "GET",
        "",
        headers={"X-State": token},
    ) == "data_bearing"


def test_egress_candidate_detection_is_bounded_and_cycle_safe():
    from app.automation.host_policy import classify_request

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    assert classify_request(
        "https://cdn.example.com/app.js",
        "GET",
        "",
        headers={"X-State": cyclic},
    ) == "data_bearing"
    assert classify_request(
        "https://cdn.example.com/app.js?state=" + ("A" * 100_000),
        "GET",
        "",
    ) == "data_bearing"


def test_unicode_allowlist_entries_are_rejected_even_for_their_idna_host():
    from app.automation.host_policy import host_matches_allowlist

    assert host_matches_allowlist(
        "xn--strae-oqa.example",
        {"straße.example"},
    ) is False


def test_invalid_egress_inputs_return_fatal_decisions_without_raising():
    from app.automation.host_policy import classify_egress

    for raw_url in (None, 17, b"https://portal.example.com/apply", "", "https://", "https://portal.example.com:bad"):
        decision = classify_egress(
            raw_url,
            "GET",
            "",
            approved_hosts={"portal.example.com"},
        )
        assert decision.allowed is False
        assert decision.fatal is True


def test_passive_custom_port_requires_exact_origin_but_loopback_explicit_origin_is_allowed():
    from app.automation.host_policy import classify_egress

    blocked = classify_egress(
        "https://portal.example.com:8443/logo.png",
        "GET",
        "",
        approved_hosts={"portal.example.com"},
    )
    assert blocked.allowed is False
    assert blocked.fatal is False
    allowed = classify_egress(
        "http://127.0.0.1:8765/logo.png",
        "GET",
        "",
        approved_hosts={"127.0.0.1"},
        approved_origins={"http://127.0.0.1:8765"},
    )
    assert allowed.allowed is True


def test_approved_origin_pii_get_and_head_are_fatal_without_explicit_data_get_approval():
    import base64
    from urllib.parse import quote

    from app.automation.host_policy import classify_egress

    encoded = quote(
        base64.urlsafe_b64encode(
            b'{"candidate":{"email":"demo.candidate@example.test","phone":"+447700900123"}}'
        ).decode("ascii"),
        safe="",
    )
    for method in ("GET", "HEAD"):
        decision = classify_egress(
            f"https://portal.example.com/assets/app.js?state={encoded}",
            method,
            "",
            approved_hosts={"portal.example.com"},
        )
        assert decision.classification == "data_bearing"
        assert decision.allowed is False
        assert decision.fatal is True
        assert decision.reason == "approved_origin_data_bearing_get_requires_explicit_approval"


def test_approved_origin_passive_resource_and_mutating_form_action_remain_allowed():
    from app.automation.host_policy import classify_egress

    passive = classify_egress(
        "https://portal.example.com/assets/app.js",
        "GET",
        "",
        approved_hosts={"portal.example.com"},
    )
    assert passive.classification == "passive_asset"
    assert passive.allowed is True

    submit = classify_egress(
        "https://portal.example.com/apply/submit",
        "POST",
        "first_name=Demo",
        approved_hosts={"portal.example.com"},
    )
    assert submit.classification == "data_bearing"
    assert submit.allowed is True


def test_ordinary_browser_navigation_headers_are_not_candidate_data():
    from app.automation.host_policy import classify_egress, classify_request

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HeadlessChrome/151.0",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "Windows",
    }
    url = "https://portal.example.com/application"
    assert classify_request(url, "GET", "", headers=headers) == "other"
    decision = classify_egress(
        url,
        "GET",
        "",
        approved_hosts={"portal.example.com"},
        headers=headers,
    )
    assert decision.allowed is True
    assert decision.fatal is False


def test_semantic_assessment_path_is_not_candidate_data():
    from app.automation.host_policy import classify_request

    assert (
        classify_request(
            "http://127.0.0.1:8787/lab/ats/assessment",
            "GET",
            "",
        )
        == "other"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://portal.example.com/assessment?email=demo%40example.test",
        "https://portal.example.com/assessment#email=demo%40example.test",
    ],
)
def test_candidate_data_in_assessment_query_or_fragment_remains_data_bearing(url: str):
    from app.automation.host_policy import classify_request

    assert classify_request(url, "GET", "") == "data_bearing"
