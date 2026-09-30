"""Fail-closed regression tests for custom-domain provider identification."""

from __future__ import annotations

from app.automation.host_policy import FIRST_PARTY_SERVICE_MANIFEST
from app.automation.targets import (
    _custom_identity_evidence,
    _detect_greenhouse_custom_domain,
    classify_target,
    trusted_provider_for_url,
)
from app.domain.targets import TargetKind


def test_self_confirming_greenhouse_evidence_is_rejected() -> None:
    evidence = {
        "provider": "greenhouse",
        "ats": "greenhouse",
        "employer": "Example Corp",
        "role": "Analyst",
        "requisition": "123",
    }

    assert _custom_identity_evidence("greenhouse", evidence) is False


def test_two_body_controlled_greenhouse_signals_are_rejected() -> None:
    url = "https://careers.example.com/jobs/123"
    html = """
    <a href="https://grnh.se/fake">Apply</a>
    <div data-job-id="123">Fake job</div>
    """

    assert _detect_greenhouse_custom_domain(url, html, {}) is None


def test_conflicting_custom_domain_vendors_are_unresolved() -> None:
    url = "https://careers.example.com/jobs/123?gh_jid=123"
    html = """
    <form action="https://boards.greenhouse.io/embed/job_app?for=example&token=123"></form>
    <iframe src="https://company.tal.net/embed/456"></iframe>
    <div data-tal-requisition="456"></div>
    """

    resolution = classify_target(
        source_url=url,
        final_url=url,
        html=html,
        identity_verified=True,
        evidence={},
    )

    assert resolution.kind in {TargetKind.UNRESOLVED, TargetKind.MISMATCH}
    assert resolution.provider != "greenhouse"
    assert "conflicting_custom_domain_providers" in resolution.reason_codes


def test_genuine_greenhouse_custom_domain_uses_two_provenance_classes() -> None:
    url = "https://careers.example.com/jobs/123?gh_jid=123"
    html = """
    <form action="https://boards.greenhouse.io/embed/job_app?for=example&token=123">
        <input name="first_name">
    </form>
    """
    evidence = {
        "ats": "greenhouse",
        "employer": "Example Corp",
        "role": "Analyst",
        "requisition": "123",
    }

    result = _detect_greenhouse_custom_domain(url, html, {})
    assert result is not None
    assert result["greenhouse_custom_domain_provenance"] == {
        "url": True,
        "vendor_endpoint": True,
        "body": False,
    }

    resolution = classify_target(
        source_url=url,
        final_url=url,
        html=html,
        identity_verified=True,
        evidence=evidence,
    )

    assert resolution.provider == "greenhouse"
    assert resolution.identity_verified is True
    assert resolution.kind == TargetKind.APPLICATION_ENTRY


def test_real_greenhouse_host_regression_is_unchanged() -> None:
    url = "https://boards.greenhouse.io/jumptrading/jobs/456789"
    resolution = classify_target(
        source_url=url,
        final_url=url,
        html='<div data-org="jumptrading"></div>',
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Jump Trading",
            "role": "Quant Researcher",
            "requisition_id": "456789",
        },
    )

    assert resolution.provider == "greenhouse"
    assert resolution.identity_verified is True
    assert trusted_provider_for_url(url) == "greenhouse"


def test_custom_domain_detection_does_not_change_egress_policy() -> None:
    before = repr(FIRST_PARTY_SERVICE_MANIFEST)
    url = "https://careers.example.com/jobs/123?gh_jid=123"
    html = '<form action="https://boards.greenhouse.io/embed/job_app?for=example&token=123"></form>'

    classify_target(
        source_url=url,
        final_url=url,
        html=html,
        identity_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Example Corp",
            "role": "Analyst",
            "requisition": "123",
        },
    )

    assert repr(FIRST_PARTY_SERVICE_MANIFEST) == before
    assert trusted_provider_for_url(url) == ""
