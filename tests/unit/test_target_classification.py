from __future__ import annotations

import pytest

from app.automation.targets import TargetResolution
from app.domain.targets import TargetKind


@pytest.mark.parametrize(
    ("source_url", "final_url", "kwargs", "kind", "provider", "reason"),
    [
        (
            "https://www.linkedin.com/jobs/search/?keywords=quant",
            "https://www.linkedin.com/jobs/view/123",
            {},
            TargetKind.LISTING,
            "",
            "linkedin_source_only",
        ),
        (
            "https://careers.example.test/brochure.pdf",
            "https://careers.example.test/brochure.pdf",
            {"content_type": "application/pdf"},
            TargetKind.NON_HTML,
            "",
            "non_html_content",
        ),
        (
            "https://careers.example.test/download",
            "https://careers.example.test/download",
            {"content_disposition": 'attachment; filename="roles.pdf"'},
            TargetKind.NON_HTML,
            "",
            "attachment_response",
        ),
        (
            "https://blackstone.wd1.myworkdayjobs.com/Blackstone_Careers",
            "https://blackstone.wd1.myworkdayjobs.com/Blackstone_Careers/jobs?q=analyst",
            {},
            TargetKind.LISTING,
            "workday",
            "search_or_listing_page",
        ),
        (
            "https://www.imc.com/eu/careers/jobs/",
            "https://www.imc.com/eu/careers/jobs/450123/quant-trading-internship/",
            {},
            TargetKind.JOB_DETAIL,
            "",
            "employer_job_detail",
        ),
        (
            "https://www.janestreet.com/join-jane-street/position/7521346002/",
            "https://www.janestreet.com/join-jane-street/position/7521346002/",
            {},
            TargetKind.JOB_DETAIL,
            "",
            "employer_job_detail",
        ),
        (
            "https://boards.greenhouse.io/acme/jobs/1234",
            "https://boards.greenhouse.io/acme/jobs/1234",
            {},
            TargetKind.APPLICATION_ENTRY,
            "greenhouse",
            "direct_ats_job",
        ),
        (
            "https://jobs.lever.co/acme/abc-def",
            "https://jobs.lever.co/acme/abc-def/apply",
            {},
            TargetKind.APPLICATION_ENTRY,
            "lever",
            "direct_ats_job",
        ),
        (
            "https://careers.example.test/role",
            "https://careers.example.test/sign-in",
            {"html": '<form id="login"><input type="password"><button>Sign in</button></form>'},
            TargetKind.AUTH_WALL,
            "",
            "authentication_wall",
        ),
        (
            "https://www.jumptrading.com/careers/",
            "https://www.jumptrading.com/careers/",
            {},
            TargetKind.LISTING,
            "",
            "search_or_listing_page",
        ),
    ],
)
def test_classify_target_is_fail_closed_for_non_application_pages(
    source_url: str,
    final_url: str,
    kwargs: dict[str, str],
    kind: TargetKind,
    provider: str,
    reason: str,
) -> None:
    from app.automation.targets import classify_target

    result = classify_target(source_url, final_url, **kwargs)

    assert result.kind is kind
    assert result.provider == provider
    assert reason in result.reason_codes
    assert result.form_verified is False
    assert result.verified_for_automation is False


def test_provider_hint_mismatch_blocks_target() -> None:
    from app.automation.targets import classify_target

    result = classify_target(
        "https://tracker.example.test/role",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="workday",
        identity_verified=True,
    )

    assert result.kind is TargetKind.MISMATCH
    assert result.identity_verified is False
    assert "provider_hint_mismatch" in result.reason_codes


def test_direct_ats_entry_requires_separate_identity_evidence_for_automation() -> None:
    from app.automation.targets import classify_target

    unverified = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
    )
    verified = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:acme"},
    )

    assert unverified.kind is TargetKind.APPLICATION_ENTRY
    assert unverified.verified_for_automation is False
    assert verified.kind is TargetKind.APPLICATION_ENTRY
    assert verified.identity_verified is True
    assert verified.verified_for_automation is True


@pytest.mark.parametrize(
    "message",
    [
        "The page you are looking for doesn't exist.",
        "The page you are looking for does not exist.",
        "This job posting is no longer available.",
    ],
)
def test_dead_workday_job_page_is_blocked_instead_of_treated_as_an_entry(
    message: str,
) -> None:
    from app.automation.targets import classify_target

    url = (
        "https://gresearch.wd103.myworkdayjobs.com/en-US/G-Research/"
        "job/Quant-Research-Internship_R36918"
    )
    result = classify_target(url, url, html=f"<main><h1>{message}</h1></main>")

    assert result.kind is TargetKind.BLOCKED
    assert result.provider == "workday"
    assert "job_closed_or_missing" in result.reason_codes
    assert result.verified_for_automation is False


def test_verified_form_handle_is_required_for_application_form_kind() -> None:
    from app.automation.targets import FormHandle, classify_target

    handle = FormHandle(
        page_id="page-1",
        frame_url="https://boards.greenhouse.io/acme/jobs/1234",
        root_selector="#application-form",
        provider="greenhouse",
        evidence={
            "control_count": 5,
            "submit_present": True,
            "root_token": "root-token-1",
            "binding_verified": True,
            "bound_target_url": "https://boards.greenhouse.io/acme/jobs/1234",
            "bound_provider": "greenhouse",
            "bound_role": "Summer Analyst",
            "bound_requisition": "REQ-1234",
            "bound_form_identity": "application-form",
        },
    )
    result = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        form_handle=handle,
    )

    assert result.kind is TargetKind.APPLICATION_FORM
    assert result.form_verified is True
    assert result.verified_for_automation is True


def test_form_handle_without_root_token_cannot_verify_application_form() -> None:
    from app.automation.targets import FormHandle, classify_target

    handle = FormHandle(
        page_id="page-without-token",
        frame_url="https://boards.greenhouse.io/acme/jobs/1234",
        root_selector="#application-form",
        provider="greenhouse",
        evidence={"control_count": 5, "submit_present": True, "root_found": True},
    )
    result = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        form_handle=handle,
    )

    assert result.kind is not TargetKind.APPLICATION_FORM
    assert result.form_verified is False
    assert result.verified_for_automation is False


def test_form_handle_without_external_resolution_binding_cannot_verify_root() -> None:
    from app.automation.targets import FormHandle, classify_target

    handle = FormHandle(
        page_id="page-without-binding",
        frame_url="https://boards.greenhouse.io/acme/jobs/1234",
        root_selector="#application-form",
        provider="greenhouse",
        evidence={
            "control_count": 5,
            "submit_present": True,
            "root_found": True,
            "root_token": "root-token-2",
        },
    )
    result = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        form_handle=handle,
    )

    assert result.kind is not TargetKind.APPLICATION_FORM
    assert result.form_verified is False
    assert result.verified_for_automation is False


def test_registry_uses_resolution_evidence_not_employer_hostname() -> None:
    from app.automation.adapters.registry import AdapterRegistry
    from app.automation.targets import classify_target

    employer_homepage = classify_target(
        "https://www.jumptrading.com/careers/",
        "https://www.jumptrading.com/careers/",
    )
    direct_greenhouse = classify_target(
        "https://boards.greenhouse.io/jumptrading/jobs/123",
        "https://boards.greenhouse.io/jumptrading/jobs/123",
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:jumptrading"},
    )

    assert AdapterRegistry().detect(employer_homepage).name == "generic"
    assert AdapterRegistry().detect(direct_greenhouse).name == "greenhouse"


def test_spoofed_provider_source_and_attacker_destination_stay_unresolved() -> None:
    from app.automation.targets import classify_target

    result = classify_target(
        "https://jobs.lever.co/acme/summer-analyst",
        "https://jobs.lever.co.attacker.test/acme/summer-analyst",
        provider_hint="lever",
        identity_verified=True,
        evidence={"source_label": "lever:acme"},
    )

    assert result.kind is TargetKind.UNRESOLVED
    assert result.provider == ""
    assert result.verified_for_automation is False


def test_trusted_provider_source_redirecting_to_attacker_is_unresolved() -> None:
    from app.automation.targets import classify_target

    result = classify_target(
        "https://jobs.lever.co/acme/summer-analyst",
        "https://attacker.example.test/acme/summer-analyst/apply",
        identity_verified=True,
    )

    assert result.kind is TargetKind.UNRESOLVED
    assert result.verified_for_automation is False


def test_custom_domain_needs_positive_ats_and_identity_evidence() -> None:
    from app.automation.targets import classify_target

    unresolved = classify_target(
        "https://careers.acme.test/jobs/summer-analyst",
        "https://careers.acme.test/jobs/summer-analyst/apply",
        html='<form data-ats="greenhouse"><input name="email"></form>',
        provider_hint="greenhouse",
        identity_verified=True,
    )
    # Page-authored DOM + caller-supplied evidence bundle is NOT independent
    # provider proof. The custom_domain_verified marker must come from the
    # detector pipeline after multi-provenance corroboration, not from the
    # page itself. This fixture previously expected a positive result; the
    # correct result is UNRESOLVED.
    verified = classify_target(
        "https://careers.acme.test/jobs/summer-analyst",
        "https://careers.acme.test/jobs/summer-analyst/apply",
        html='<form data-ats="greenhouse" data-employer="Acme" data-role="Summer Analyst">'
        '<input name="email"><input name="requisition" value="REQ-7"></form>',
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={
            "ats_verified": "greenhouse",
            "employer": "Acme",
            "role": "Summer Analyst",
            "requisition": "REQ-7",
            "form_identity": "application-form",
        },
    )

    assert unresolved.kind is TargetKind.UNRESOLVED
    assert unresolved.verified_for_automation is False
    assert verified.kind is TargetKind.UNRESOLVED
    assert verified.verified_for_automation is False
    assert "custom_domain_identity_unverified" in verified.reason_codes

    # POSITIVE CONTROL: the trusted-host path is separate and unaffected --
    # a real Greenhouse host with structured-feed proof still resolves and
    # still selects the greenhouse adapter.
    trusted = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:acme"},
    )
    assert trusted.kind is TargetKind.APPLICATION_ENTRY
    assert trusted.provider == "greenhouse"
    assert trusted.verified_for_automation is True


# SECURITY: on a custom (non-trusted) domain every identity value in a
# page-supplied bundle (ats/provider, employer, role, requisition, form id)
# is read from the employer's own page DOM/JSON-LD and is therefore
# attacker-controllable (see app/services/navigator.py:1716-1930
# `_page_observation`). `ats == provider` inside the same bundle is the page
# agreeing with itself, not independent corroboration, so a bare bundle must
# never prove the ATS. Do NOT "fix" a red test here by restoring
# self-corroboration; the hardening that removed it is correct.
def test_custom_domain_identity_bundle_alone_does_not_prove_ats() -> None:
    from app.automation.adapters.registry import AdapterRegistry
    from app.automation.targets import classify_target

    result = classify_target(
        "https://careers.acme.test/jobs/summer-analyst",
        "https://careers.acme.test/jobs/summer-analyst/apply",
        identity_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Acme",
            "role": "Summer Analyst",
            "requisition_id": "REQ-8",
            "form_id": "application-form",
        },
    )

    # The page-controlled bundle alone proves nothing: no automation claim
    # and no vendor adapter. (The `provider` label may echo the page claim,
    # but it carries no authority: verified_for_automation is False and the
    # registry falls back to generic.)
    assert result.kind in (TargetKind.UNRESOLVED, TargetKind.MISMATCH)
    assert result.verified_for_automation is False
    assert AdapterRegistry().detect(result).name == "generic"

    # POSITIVE CONTROL: the trusted-host path is separate and unaffected --
    # a real Greenhouse host with structured-feed proof still resolves and
    # still selects the greenhouse adapter.
    trusted = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:acme"},
    )
    assert trusted.kind is TargetKind.APPLICATION_ENTRY
    assert trusted.provider == "greenhouse"
    assert trusted.verified_for_automation is True
    assert AdapterRegistry().detect(trusted).name == "greenhouse"


# SECURITY: same reason as above -- a page-supplied identity bundle on a
# custom domain cannot corroborate itself (app/services/navigator.py:1716-1930).
# The registry must fall back to generic unless independent proof exists.
def test_registry_denies_custom_domain_identity_bundle_without_independent_proof() -> None:
    from app.automation.adapters.registry import AdapterRegistry
    from app.automation.targets import classify_target

    result = classify_target(
        "https://careers.acme.test/jobs/summer-analyst",
        "https://careers.acme.test/jobs/summer-analyst/apply",
        identity_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Acme",
            "role": "Summer Analyst",
            "requisition_id": "REQ-8",
            "form_id": "application-form",
        },
    )

    assert result.verified_for_automation is False
    assert AdapterRegistry().detect(result).name == "generic"

    # POSITIVE CONTROL: trusted Greenhouse host still enables its adapter.
    trusted = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
        provider_hint="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:acme"},
    )
    assert AdapterRegistry().detect(trusted).name == "greenhouse"


# SECURITY: a hand-constructed TargetResolution carrying only a
# page-controllable identity bundle is not independent proof of the ATS
# (app/services/navigator.py:1716-1930). The registry must not enable a
# vendor adapter for it. This shape is not produced by any production
# custom-domain caller.
def test_registry_denies_handbuilt_custom_resolution_identity_bundle() -> None:
    from app.automation.adapters.registry import AdapterRegistry

    result = TargetResolution(
        source_url="https://careers.acme.test/job",
        final_url="https://careers.acme.test/job/apply",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Acme",
            "role": "Summer Analyst",
            "requisition_id": "REQ-8",
            "form_id": "application-form",
        },
    )

    assert result.verified_for_automation is False
    assert AdapterRegistry().detect(result).name == "generic"

    # POSITIVE CONTROL: a trusted-host resolution with structured-feed proof
    # still enables the greenhouse adapter.
    trusted = TargetResolution(
        source_url="https://boards.greenhouse.io/acme/jobs/1234",
        final_url="https://boards.greenhouse.io/acme/jobs/1234",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:acme"},
    )
    assert trusted.verified_for_automation is True
    assert AdapterRegistry().detect(trusted).name == "greenhouse"


def test_registry_returns_generic_for_unverified_hosted_ats_resolution() -> None:
    from app.automation.adapters.registry import AdapterRegistry
    from app.automation.targets import classify_target

    result = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234",
    )

    assert result.kind is TargetKind.APPLICATION_ENTRY
    assert result.verified_for_automation is False
    assert AdapterRegistry().detect(result).name == "generic"


def test_registry_rejects_provider_claim_on_untrusted_final_host() -> None:
    from app.automation.adapters.registry import AdapterRegistry

    forged = TargetResolution(
        source_url="https://attacker.test/job",
        final_url="https://boards.greenhouse.io.attacker.test/acme/jobs/1234",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"structured_feed": "greenhouse:acme"},
    )

    assert AdapterRegistry().detect(forged).name == "generic"


def test_registry_does_not_treat_a_bare_form_id_as_verified_form_evidence() -> None:
    from app.automation.adapters.registry import AdapterRegistry

    forged = TargetResolution(
        source_url="https://boards.greenhouse.io/acme/jobs/1234",
        final_url="https://boards.greenhouse.io/acme/jobs/1234",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"form_id": "newsletter"},
    )

    assert AdapterRegistry().detect(forged).name == "generic"


def test_target_resolution_evidence_is_deeply_immutable() -> None:
    from app.automation.targets import TargetResolution

    result = TargetResolution(
        source_url="https://careers.example.test/job",
        final_url="https://careers.example.test/job",
        kind=TargetKind.JOB_DETAIL,
        evidence={"nested": {"items": [{"role": "Analyst"}]}},
    )

    with pytest.raises(TypeError):
        result.evidence["nested"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        result.evidence["nested"]["items"] = ()  # type: ignore[index]
    with pytest.raises(AttributeError):
        result.evidence["nested"]["items"].append({})  # type: ignore[union-attr]


def test_form_handle_uses_canonical_target_contract_url_for_equivalent_bindings() -> None:
    from app.automation.targets import FormHandle, classify_target

    handle = FormHandle(
        page_id="page-equivalent-url",
        frame_url="https://boards.greenhouse.io:443/acme/jobs/1234/?state=ready%20now",
        root_selector="#application-form",
        provider="greenhouse",
        evidence={
            "control_count": 1,
            "submit_present": True,
            "root_token": "application-form",
            "binding_verified": True,
            "bound_target_url": "https://boards.greenhouse.io/acme/jobs/1234/?state=ready%20now",
            "bound_provider": "greenhouse",
        },
    )
    result = classify_target(
        "https://boards.greenhouse.io/acme/jobs/1234",
        "https://boards.greenhouse.io/acme/jobs/1234?state=ready+now",
        provider_hint="greenhouse",
        identity_verified=True,
        form_handle=handle,
        evidence={"structured_feed": "greenhouse:acme"},
    )

    assert result.kind is TargetKind.APPLICATION_FORM
    assert result.form_verified is True
    assert result.verified_for_automation is True
