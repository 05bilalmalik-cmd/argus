"""Tests for Phase 39 — same-registrable-domain passive_asset rule.

The rule classifies a blocked request as FIRST_PARTY_SERVICE (essential) when
ALL of these hold:

1. policy_classification == "passive_asset"
2. resource_type in {script, stylesheet, font, image, other}
3. request host shares registrable domain with the verified target URL
   (current_page_url — the posting being filled)
4. mode is PREFILL

This fires on the production path i.e. when resolved_vendor IS supplied.
"""

from __future__ import annotations

import pytest

from app.automation.host_policy import BlockedRequestImpact, classify_blocked_request_impact
from app.automation.types import RunMode


# ===================================================================
# Group 1 — passive assets on same registrable domain as job posting
# ===================================================================


@pytest.mark.parametrize(
    (
        "url",
        "method",
        "resource_type",
        "policy_classification",
        "current_page_url",
    ),
    [
        # Content-hashed React CSS bundles
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/entry-ZTzpC0b7.css",
            "GET",
            "stylesheet",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-css-bundle",
        ),
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/vendor-da5IcPkB.css",
            "GET",
            "stylesheet",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-vendor-css",
        ),
        # Content-hashed React JS bundles
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/manifest-d11fcd84.js",
            "GET",
            "script",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-manifest-js",
        ),
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/entry.client-D8ZlKSVO.js",
            "GET",
            "script",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-entry-client",
        ),
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/vendor-Bit_RVLI.js",
            "GET",
            "script",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-vendor-js",
        ),
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/root-YkWblwLz.js",
            "GET",
            "script",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-root-js",
        ),
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/assets/_url_token_.jobs_._job_post_id-BQ61QEgQ.js",
            "GET",
            "script",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="react-route-chunk",
        ),
        # CDN image on s7-recruiting subdomain
        pytest.param(
            "https://s7-recruiting.cdn.greenhouse.io/assets/LionTree_Logo_Horizontal.png",
            "GET",
            "image",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="cdn-recruiting-image",
        ),
        # CDN font — subsumed by new rule from old manifest prefix-entries
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/fonts/UntitledSansWeb-Regular.woff",
            "GET",
            "font",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="cdn-font",
        ),
        # CDN font woff2 — same
        pytest.param(
            "https://job-boards.cdn.greenhouse.io/fonts/OpenSans-Variable.woff2",
            "GET",
            "font",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="cdn-font-woff2",
        ),
        # Other CDN host within same registrable domain
        pytest.param(
            "https://job-boards.eu.greenhouse.io/assets/some-style.css",
            "GET",
            "stylesheet",
            "passive_asset",
            "https://boards.greenhouse.io/acme/jobs/1234567",
            id="eu-cdn-stylesheet",
        ),
    ],
)
def test_same_rd_passive_asset_is_first_party_service(
    url: str,
    method: str,
    resource_type: str,
    policy_classification: str,
    current_page_url: str,
) -> None:
    """A passive asset on the same registrable domain as the job posting
    must be FIRST_PARTY_SERVICE (essential) on the PRODUCTION path with
    resolved_vendor supplied."""
    decision = classify_blocked_request_impact(
        url=url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification=policy_classification,
        resolved_vendor="greenhouse",
        current_page_url=current_page_url,
    )

    assert decision.impact is BlockedRequestImpact.FIRST_PARTY_SERVICE
    assert decision.reason == "first_party_service_on_same_registrable_domain"
    assert decision.consequence.value == "essential"


# ===================================================================
# Group 2 — Things that MUST stay FATAL
# ===================================================================


def test_data_bearing_get_on_same_rd_via_manifest() -> None:
    """A data-bearing GET (candidate data in query) on the same registrable
    domain must remain FATAL unless the exact self-service endpoint is manifested."""
    decision = classify_blocked_request_impact(
        url="https://my.greenhouse.io/users/self?job_post_id=5213296007",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="data_bearing",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    # The manifest entry has candidate_data_kind="" so it IS recognised
    # as a first-party service endpoint despite the data_bearing classification.
    assert decision.impact is BlockedRequestImpact.FIRST_PARTY_SERVICE
    assert decision.reason == "first_party_service_manifest_match"


def test_post_on_same_rd_stays_fatal() -> None:
    """Any POST/PUT/PATCH/DELETE on the same registrable domain as the job
    posting is NOT a passive asset and must stay FATAL."""
    decision = classify_blocked_request_impact(
        url="https://boards.greenhouse.io/applications",
        method="POST",
        resource_type="fetch",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="data_bearing",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    # POST is data_bearing, no manifest entry matched
    assert decision.reason == "data_bearing_request_blocked"


def test_passive_asset_different_rd_stays_fatal() -> None:
    """A passive asset on a DIFFERENT registrable domain from the job posting
    with no OPTIONAL consequence entry stays FATAL."""
    decision = classify_blocked_request_impact(
        url="https://evil-cdn.example.com/assets/tracker.js",
        method="GET",
        resource_type="script",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "unknown_resource_blocked"


def test_fetch_resource_type_on_same_rd_stays_fatal() -> None:
    """A `fetch`/`xhr` resource type on the same registrable domain is NOT
    in the allowed resource-type set (script/stylesheet/font/image/other)
    and must stay FATAL.  Only passive_asset is subject to the new rule."""
    decision = classify_blocked_request_impact(
        url="https://job-boards.cdn.greenhouse.io/api/endpoint",
        method="GET",
        resource_type="fetch",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="other",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "unknown_resource_blocked"


@pytest.mark.parametrize(
    ("mode",),
    [
        pytest.param(RunMode.REVIEW.value, id="review"),
        pytest.param(RunMode.SUBMIT.value, id="submit"),
    ],
)
def test_passive_asset_not_prefill_stays_fatal(mode: str) -> None:
    """The same passive asset on the same registrable domain when mode is
    REVIEW or SUBMIT must be FATAL — the rule only applies in PREFILL."""
    decision = classify_blocked_request_impact(
        url="https://job-boards.cdn.greenhouse.io/assets/entry-ZTzpC0b7.css",
        method="GET",
        resource_type="stylesheet",
        is_navigation_request=False,
        mode=mode,
        policy_classification="passive_asset",
        resolved_vendor="greenhouse",
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "impact_classification_not_prefill"