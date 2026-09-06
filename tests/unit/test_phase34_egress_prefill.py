"""Tests for Phase 34 egress prefill fixes — Group 1 (same-registrable-domain),
Group 2 (CAPTCHA), and Group 3 (Google Drive file picker).  Every test calls
``classify_blocked_request_impact`` directly.

ASSERTS that each group's blocked resources are now TOLERABLE, and that the
existing FATAL guardrails for truly dangerous requests stay FATAL.
"""

from __future__ import annotations

import pytest

from app.automation.host_policy import (
    BlockedRequestImpact,
    _registrable_domain,
    classify_blocked_request_impact,
)
from app.automation.types import RunMode


# ---------------------------------------------------------------------------
# Group 1 — same registrable domain as approved host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "resource_type", "method", "policy_classification"),
    [
        # locale JSON (fetch — labelled "other" because .json is not a
        # passive suffix and path has no passive hint)
        (
            "https://job-boards.cdn.greenhouse.io/locales/en/job_post.a1b2c3.json",
            "fetch",
            "GET",
            "other",
        ),
        (
            "https://job-boards.cdn.greenhouse.io/locales/en/common.d4e5f6.json",
            "fetch",
            "GET",
            "other",
        ),
        # CDN fonts
        (
            "https://job-boards.cdn.greenhouse.io/fonts/UntitledSansWeb-Regular.woff",
            "font",
            "GET",
            "passive_asset",
        ),
        # my.greenhouse.io — session endpoint
        (
            "https://my.greenhouse.io/users/self",
            "fetch",
            "GET",
            "other",
        ),
        # boards.greenhouse.io — presigned fields for resume upload
        (
            "https://boards.greenhouse.io/uncacheable_attributes/presigned_fields"
            "?fields%5B%5D=resume",
            "fetch",
            "GET",
            "data_bearing",
        ),
    ],
)
def test_same_registrable_domain_is_tolerable(
    url: str,
    resource_type: str,
    method: str,
    policy_classification: str,
) -> None:
    """A blocked request on the same registrable domain as an approved host
    is TOLERABLE (the prefill continues)."""
    decision = classify_blocked_request_impact(
        url=url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification=policy_classification,
        approved_hosts={"job-boards.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE
    assert decision.reason == "first_party_service_on_same_registrable_domain"
    assert decision.consequence.value == "optional"


@pytest.mark.parametrize(
    "url",
    [
        # DIFFERENT registrable domain from approved — not tolerable
        "https://evil-cdn.example.com/assets/logo.png",
        "https://not-greenhouse.io/users/self",
        "https://job-boards.greenhouse.io.evil.com/steal",
        # registrable-domain mismatch: greenhouse.com vs greenhouse.io
        "https://cdn.greenhouse.com/assets/logo.png",
    ],
)
def test_different_registrable_domain_stays_fatal(url: str) -> None:
    """A host on a DIFFERENT registrable domain from every approved host
    remains FATAL with unknown_resource_blocked."""
    decision = classify_blocked_request_impact(
        url=url,
        method="GET",
        resource_type="image",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        approved_hosts={"job-boards.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "unknown_resource_blocked"
    assert decision.consequence.value == "unknown"


# ---------------------------------------------------------------------------
# Group 2 — CAPTCHA provider resources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        # recaptcha.net enterprise
        (
            "https://www.recaptcha.net/recaptcha/enterprise.js",
            "script",
        ),
        (
            "https://www.recaptcha.net/recaptcha/enterprise/anchor",
            "document",
        ),
        # Google recaptcha
        (
            "https://www.google.com/recaptcha/api2/anchor",
            "document",
        ),
        # gstatic recaptcha assets
        (
            "https://www.gstatic.com/recaptcha/releases/abc123/recaptcha__en.js",
            "script",
        ),
        (
            "https://www.gstatic.com/recaptcha/releases/abc123/styles__ltr.css",
            "stylesheet",
        ),
        (
            "https://www.gstatic.com/recaptcha/api2/logo_48.png",
            "image",
        ),
    ],
)
def test_captcha_provider_resource_is_tolerable(
    url: str,
    resource_type: str,
) -> None:
    """A known CAPTCHA provider resource (matched by host + path prefix +
    resource_type from _CAPTCHA_PROVIDER_PATH_RULES) should be TOLERABLE."""
    decision = classify_blocked_request_impact(
        url=url,
        method="GET",
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE
    assert decision.reason == "captcha_provider_resource_blocked"
    assert decision.consequence.value == "optional"


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        # Recaptcha path NOT matching path rules
        (
            "https://www.recaptcha.net/recaptcha/unknown-path.js",
            "script",
        ),
        # Wrong resource type for the matched path prefix
        (
            "https://www.recaptcha.net/recaptcha/enterprise.js",
            "image",
        ),
        # CAPTCHA host but no recognized path
        (
            "https://www.google.com/unrelated/file.js",
            "script",
        ),
        # Non-CAPTCHA host
        (
            "https://evil.com/recaptcha/api2/anchor",
            "document",
        ),
    ],
)
def test_nonmatching_captcha_stays_fatal(
    url: str,
    resource_type: str,
) -> None:
    """A request to a non-CAPTCHA host or to a CAPTCHA host with a
    non-matching path/resource-type stays FATAL."""
    decision = classify_blocked_request_impact(
        url=url,
        method="GET",
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
    )

    assert decision.impact is BlockedRequestImpact.FATAL


# ---------------------------------------------------------------------------
# Group 3 — Google Drive file picker resources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        (
            "https://apis.google.com/js/googleapis.proxy.js",
            "script",
        ),
        (
            "https://content.googleapis.com/static/proxy.html",
            "document",
        ),
        (
            "https://content.googleapis.com/discovery/v1/apis/drive/v3/rest",
            "fetch",
        ),
    ],
)
def test_google_drive_file_picker_is_tolerable(
    url: str,
    resource_type: str,
) -> None:
    """Google Drive file picker entries in the consequence table should
    be TOLERABLE (same treatment as Dropbox)."""
    decision = classify_blocked_request_impact(
        url=url,
        method="GET",
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE
    assert decision.reason == "optional_third_party_file_picker"
    assert decision.consequence.value == "optional"


# ---------------------------------------------------------------------------
# Things that must STAY FATAL
# ---------------------------------------------------------------------------


def test_unapproved_unknown_script_stays_fatal() -> None:
    """An unknown script on an unapproved host must remain FATAL."""
    decision = classify_blocked_request_impact(
        url="https://evil-script.example.com/tracker.js",
        method="GET",
        resource_type="script",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        approved_hosts={"job-boards.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "unknown_resource_blocked"


def test_data_bearing_post_stays_fatal() -> None:
    """A data-bearing POST carrying candidate data to any host must be FATAL."""
    decision = classify_blocked_request_impact(
        url="https://analytics.example.com/collect",
        method="POST",
        resource_type="fetch",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="data_bearing",
        carries_candidate_data=True,
        approved_hosts={"job-boards.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "data_bearing_request_blocked"


def test_any_request_not_in_prefill_mode_is_fatal() -> None:
    """Any request when mode is not 'prefill' must be FATAL."""
    for mode in (RunMode.REVIEW.value, RunMode.SUBMIT.value, ""):
        decision = classify_blocked_request_impact(
            url="https://job-boards.cdn.greenhouse.io/locales/en/common.json",
            method="GET",
            resource_type="fetch",
            is_navigation_request=False,
            mode=mode,
            policy_classification="other",
            approved_hosts={"job-boards.greenhouse.io"},
        )

        assert decision.impact is BlockedRequestImpact.FATAL
        assert decision.reason == "impact_classification_not_prefill"


# ---------------------------------------------------------------------------
# Helper function smoketests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("greenhouse.io", "greenhouse.io"),
        ("job-boards.greenhouse.io", "greenhouse.io"),
        ("job-boards.cdn.greenhouse.io", "greenhouse.io"),
        ("www.recaptcha.net", "recaptcha.net"),
        ("www.gstatic.com", "gstatic.com"),
        ("localhost", None),
        ("127.0.0.1", None),
        ("", None),
    ],
)
def test_registrable_domain_extraction(
    hostname: str,
    expected: str | None,
) -> None:
    assert _registrable_domain(hostname) == expected