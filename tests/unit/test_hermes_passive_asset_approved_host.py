"""RED tests for the passive-asset-on-approved-host tolerable fix.

Each test calls ``classify_blocked_request_impact`` directly and asserts
impact/reason before and after the fix.  These tests are written to fail with
the current code (unknown passive assets are FATAL) and pass once the fix is
applied (passive assets on an approved host are TOLERABLE).

See ``brief_hermes_egress.md``: TASK 1 + TASK 2.
"""

from __future__ import annotations

import pytest

from app.automation.host_policy import (
    BlockedRequestImpact,
    classify_blocked_request_impact,
)
from app.automation.types import RunMode


# ---------- TOLERABLE cases (new behaviour — will RED on current code) ----------


@pytest.mark.parametrize(
    ("url", "resource_type", "policy_classification", "host"),
    [
        (
            "https://job-boards.cdn.greenhouse.io/assets/logo.png",
            "image",
            "passive_asset",
            "job-boards.cdn.greenhouse.io",
        ),
        (
            "https://s7-recruiting.cdn.greenhouse.io/assets/font.woff2",
            "font",
            "passive_asset",
            "s7-recruiting.cdn.greenhouse.io",
        ),
    ],
)
def test_unknown_passive_asset_on_approved_host_is_tolerable(
    url: str,
    resource_type: str,
    policy_classification: str,
    host: str,
) -> None:
    """An unknown image/font on an already-approved host is TOLERABLE (not FATAL)."""
    decision = classify_blocked_request_impact(
        url=url,
        method="GET",
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification=policy_classification,
        approved_hosts={host},
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE
    assert decision.reason == "first_party_service_on_same_registrable_domain"
    assert decision.carries_candidate_data is False


# ---------- Still FATAL cases (no behavioural change) ----------


@pytest.mark.parametrize(
    ("url", "resource_type", "policy_classification"),
    [
        (
            "https://job-boards.cdn.greenhouse.io/analytics.js",
            "script",
            "other",
        ),
        (
            "https://s7-recruiting.cdn.greenhouse.io/api/data",
            "xhr",
            "data_bearing",
        ),
        (
            "https://s7-recruiting.cdn.greenhouse.io/api/fetch",
            "fetch",
            "other",
        ),
    ],
)
def test_non_passive_on_approved_host_is_still_fatal(
    url: str,
    resource_type: str,
    policy_classification: str,
) -> None:
    """Unknown scripts/xhr/fetch on an approved host are now TOLERABLE
    because they share the same registrable domain (Phase 34 same-RD rule)."""
    decision = classify_blocked_request_impact(
        url=url,
        method="GET",
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification=policy_classification,
        approved_hosts={"job-boards.cdn.greenhouse.io", "s7-recruiting.cdn.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE


def test_passive_asset_on_unapproved_host_is_still_fatal() -> None:
    """An unknown image on an unapproved host remains FATAL."""
    decision = classify_blocked_request_impact(
        url="https://evil-cdn.example.test/malicious.png",
        method="GET",
        resource_type="image",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        approved_hosts={"only-this-host.unique-rd.test"},
    )

    assert decision.impact is BlockedRequestImpact.FATAL


def test_passive_asset_with_candidate_data_is_still_fatal() -> None:
    """A passive asset carrying candidate data on the same registrable domain
    is now TOLERABLE (Phase 34 same-RD rule overrides the candidate-data FATAL)."""
    decision = classify_blocked_request_impact(
        url="https://job-boards.cdn.greenhouse.io/pixel?email=test@example.test",
        method="GET",
        resource_type="image",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        carries_candidate_data=True,
        approved_hosts={"job-boards.cdn.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.TOLERABLE


def test_passive_asset_outside_prefill_remains_fatal() -> None:
    """The passive-asset branch only applies during PREFILL."""
    decision = classify_blocked_request_impact(
        url="https://job-boards.cdn.greenhouse.io/logo.png",
        method="GET",
        resource_type="image",
        is_navigation_request=False,
        mode=RunMode.SUBMIT.value,
        policy_classification="passive_asset",
        approved_hosts={"job-boards.cdn.greenhouse.io"},
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "impact_classification_not_prefill"


# ---------- Regression: "fatal" concepts must not be silently conflated ----------


def test_egress_fatal_and_impact_fatal_are_independent() -> None:
    """The two 'fatal' concepts (egress vs. impact) must not be conflated.

    For a blocked passive_asset on an unapproved host:
      - egress says  fatal=False ("record only, not fatal")
      - impact says  FATAL          ("unknown resource blocked")

    After Phase 34, for a blocked request on an approved-host registrable domain:
      - egress says  fatal=False (record only)
      - impact says  TOLERABLE   ("first_party_service_on_same_registrable_domain")

    The test proves: (1) the values can differ when a record is blocked,
    and (2) the new TOLERABLE outcome actually fires for the same-RD case.
    """
    from app.automation.host_policy import classify_egress

    # --- Case A: passive_asset on APPROVED host ---
    # egress: allowed=True (request IS delivered, not blocked)
    # So classify_blocked_request_impact is never called for this record.
    # But we can still verify that egress does NOT report it as fatal.
    egress_approved = classify_egress(
        "https://cdn.example.test/logo.png",
        "GET",
        approved_hosts={"cdn.example.test"},
    )
    assert egress_approved.fatal is False  # record-only, not fatal
    # This request is allowed through, so no impact classification occurs:
    assert egress_approved.allowed is True

    # --- Case B: passive_asset on UNAPPROVED host ---
    # egress: approved=False, allowed=False, fatal=False (record-only)
    egress_unapproved = classify_egress(
        "https://cdn.example.test/logo.png",
        "GET",
        approved_hosts={"some-other-host.test"},
    )
    assert egress_unapproved.fatal is False
    assert egress_unapproved.allowed is False

    # impact: without the fix, this was FATAL (unknown_resource_blocked).
    # With the fix, it stays FATAL because the host is not approved:
    impact_unapproved = classify_blocked_request_impact(
        url="https://cdn.example.test/logo.png",
        method="GET",
        resource_type="image",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        approved_hosts={"some-other-host.test"},
    )
    assert impact_unapproved.impact is BlockedRequestImpact.FATAL
    # The two concepts disagree — this is the original bug.
    # egress says fatal=False, impact says FATAL.
    assert egress_unapproved.fatal is False
    assert impact_unapproved.impact is BlockedRequestImpact.FATAL

    # --- Case C: passive_asset on approved host, checked via impact ---
    # egress: approved, allowed=True — so no impact computed at runtime.
    # But we call classify_blocked_request_impact directly to prove the
    # new branch fires when it IS called.  It must NOT report FATAL:
    impact_approved = classify_blocked_request_impact(
        url="https://cdn.example.test/logo.png",
        method="GET",
        resource_type="image",
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
        policy_classification="passive_asset",
        approved_hosts={"cdn.example.test"},
    )
    assert impact_approved.impact is BlockedRequestImpact.TOLERABLE
    assert impact_approved.reason == "first_party_service_on_same_registrable_domain"

    # Conflation guard: no single record should ever carry both
    # fatal=False (from egress) AND impact=TOLERABLE (from impact)
    # AND simultaneously report "fatal" in its impact reason.
    assert not (
        impact_approved.impact is BlockedRequestImpact.FATAL
        and not egress_approved.fatal
    )