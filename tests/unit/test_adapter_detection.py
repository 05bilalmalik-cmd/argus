from app.automation.adapters.registry import AdapterRegistry
from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.adapters.lever import LeverAdapter
from app.automation.adapters.workday import WorkdayAdapter
from app.automation.targets import TargetResolution, trusted_provider_for_url
from app.domain.targets import TargetKind


def test_registry_detects_greenhouse_from_hostname() -> None:
    unverified = TargetResolution(
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        final_url="https://boards.greenhouse.io/acme/jobs/123",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=False,
    )
    assert AdapterRegistry().detect(unverified).name == "generic"


def test_registry_detects_lever_from_hostname() -> None:
    unverified = TargetResolution(
        source_url="https://jobs.lever.co/acme/abc",
        final_url="https://jobs.lever.co/acme/abc",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="lever",
        identity_verified=False,
    )
    assert AdapterRegistry().detect(unverified).name == "generic"


def test_registry_detects_workday_from_hostname() -> None:
    unverified = TargetResolution(
        source_url="https://acme.wd3.myworkdayjobs.com/en-US/jobs/1",
        final_url="https://acme.wd3.myworkdayjobs.com/en-US/jobs/1",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="workday",
        identity_verified=False,
    )
    assert AdapterRegistry().detect(unverified).name == "generic"


def test_registry_detects_provider_only_after_verified_resolution() -> None:
    registry = AdapterRegistry()
    for provider, url in (
        ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123"),
        ("lever", "https://jobs.lever.co/acme/abc"),
        ("workday", "https://acme.wd3.myworkdayjobs.com/en-US/jobs/1"),
    ):
        result = TargetResolution(
            source_url=url,
            final_url=url,
            kind=TargetKind.APPLICATION_ENTRY,
            provider=provider,
            identity_verified=True,
            evidence={"structured_feed": f"{provider}:acme"},
        )
        assert registry.detect(result).name == provider


def test_greenhouse_matcher_rejects_suffix_spoof_hosts() -> None:
    assert not GreenhouseAdapter.matches("https://boards.greenhouse.io.attacker.test/jobs/123")


def test_greenhouse_matcher_accepts_the_official_european_job_board() -> None:
    url = "https://job-boards.eu.greenhouse.io/isam/jobs/4949792101"

    assert trusted_provider_for_url(url) == "greenhouse"
    assert GreenhouseAdapter.matches(url)


def test_lever_matcher_requires_trusted_provider_host_and_ignores_dom_marker() -> None:
    assert LeverAdapter.matches("https://jobs.lever.co/acme/abc")
    assert not LeverAdapter.matches("https://acme.lever.co/jobs/abc")
    assert not LeverAdapter.matches(
        "https://careers.example.test/jobs/abc",
        '<main data-ats="lever"></main>',
    )


def test_workday_matcher_requires_trusted_provider_host_and_ignores_dom_marker() -> None:
    assert WorkdayAdapter.matches("https://acme.wd3.myworkdayjobs.com/en-US/jobs/1")
    assert not WorkdayAdapter.matches("https://acme.myworkdayjobs.com/en-US/jobs/1")
    assert not WorkdayAdapter.matches(
        "https://careers.example.test/jobs/1",
        '<main data-ats="workday"></main>',
    )


def test_provider_trust_requires_https_default_port() -> None:
    assert trusted_provider_for_url("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert trusted_provider_for_url("https://boards.greenhouse.io:443/acme/jobs/123") == "greenhouse"
    assert trusted_provider_for_url("http://boards.greenhouse.io/acme/jobs/123") == ""
    assert trusted_provider_for_url("https://boards.greenhouse.io:8443/acme/jobs/123") == ""
    assert AdapterRegistry().detect(
        TargetResolution(
            source_url="https://boards.greenhouse.io:8443/acme/jobs/123",
            final_url="https://boards.greenhouse.io:8443/acme/jobs/123",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            evidence={"structured_feed": "greenhouse:acme"},
        )
    ).name == "generic"


def test_off_origin_official_hostname_cannot_use_custom_identity_to_enable_adapter() -> None:
    result = TargetResolution(
        source_url="https://boards.greenhouse.io:8443/acme/jobs/123",
        final_url="https://boards.greenhouse.io:8443/acme/jobs/123/apply",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "ats": "greenhouse",
            "employer": "Acme",
            "role": "Summer Analyst",
            "requisition_id": "REQ-123",
            "form_id": "application-form",
        },
    )

    assert result.verified_for_automation is False
    assert AdapterRegistry().detect(result).name == "generic"


def test_registry_uses_dom_marker_then_generic_fallback() -> None:
    registry = AdapterRegistry()

    assert registry.detect("https://careers.example.test/job", '<main data-ats="greenhouse">').name == "generic"
    assert registry.detect("https://careers.example.test/job", "<form></form>").name == "generic"


def _provider_target(url: str, provider: str, verified: bool = True) -> TargetResolution:
    return TargetResolution(
        source_url=url, final_url=url, kind=TargetKind.APPLICATION_ENTRY,
        provider=provider, identity_verified=verified,
        evidence={"structured_feed": f"{provider}:acme"},
    )


def test_smartrecruiters_requires_verified_jobs_host():
    verified = _provider_target("https://jobs.smartrecruiters.com/acme/123-role", "smartrecruiters")
    assert AdapterRegistry().detect(verified).name == "smartrecruiters"
    assert AdapterRegistry().detect(TargetResolution(
        source_url=verified.final_url, final_url=verified.final_url,
        kind=TargetKind.APPLICATION_ENTRY, provider="smartrecruiters",
        identity_verified=False, evidence={"structured_feed": "smartrecruiters:acme"},
    )).name == "generic"


def test_workable_requires_verified_apply_host():
    verified = _provider_target("https://apply.workable.com/acme/j/ABC123/", "workable")
    assert AdapterRegistry().detect(verified).name == "workable"


def test_new_provider_trust_is_exact_https_default_port_only():
    assert trusted_provider_for_url("https://jobs.smartrecruiters.com/acme/123") == "smartrecruiters"
    assert trusted_provider_for_url("https://apply.workable.com/acme/j/ABC/") == "workable"
    assert trusted_provider_for_url("http://jobs.smartrecruiters.com/acme/123") == ""
    assert trusted_provider_for_url("https://apply.workable.com:8443/acme/j/ABC/") == ""
    assert trusted_provider_for_url("https://jobs.smartrecruiters.com.attacker.test/acme/123") == ""
    assert trusted_provider_for_url("https://careers.example.test/jobs/smartrecruiters") == ""


def test_new_provider_detection_ignores_labels_and_html():
    for url, provider in (("https://careers.example.test/jobs/smartrecruiters", "smartrecruiters"),
                          ("https://careers.example.test/jobs/workable", "workable")):
        result = TargetResolution(source_url=url, final_url=url, kind=TargetKind.APPLICATION_ENTRY,
                                  provider=provider, identity_verified=True,
                                  evidence={"structured_feed": f"{provider}:acme"})
        assert AdapterRegistry().detect(result, f'<main data-ats="{provider}">').name == "generic"


def test_new_provider_custom_marketing_bundle_cannot_enable_adapter():
    for provider, url in (("smartrecruiters", "https://careers.acme.example/jobs/123"), ("workable", "https://jobs.acme.example/analyst")):
        result = TargetResolution(
            source_url=url, final_url=url, kind=TargetKind.APPLICATION_ENTRY, provider=provider, identity_verified=True,
            evidence={"ats": provider, "employer": "Acme", "role": "Summer Analyst", "requisition_id": "REQ-123", "form_id": "application-form"},
        )
        assert result.verified_for_automation is True
        assert AdapterRegistry().detect(result).name == "generic"


def test_new_provider_loopback_exception_requires_synthetic_lab_evidence():
    for provider in ("smartrecruiters", "workable"):
        for evidence in ({}, {"synthetic_lab": True}):
            result = TargetResolution(
                source_url="http://127.0.0.1:8787/lab/ats/journey", final_url="http://127.0.0.1:8787/lab/ats/journey",
                kind=TargetKind.APPLICATION_ENTRY, provider=provider, identity_verified=True, evidence=evidence,
            )
            expected = provider if evidence.get("synthetic_lab") else "generic"
            assert AdapterRegistry().detect(result).name == expected



def test_loopback_synthetic_authorization_requires_exact_true_boolean():
    false_like = (None, False, 0, 1, "true", "false", "", "lab", [], {}, ["x"], {"x": 1})
    for provider in ("smartrecruiters", "workable"):
        for value in false_like:
            result = TargetResolution(
                source_url="http://127.0.0.1:8787/lab/ats/journey",
                final_url="http://127.0.0.1:8787/lab/ats/journey",
                kind=TargetKind.APPLICATION_ENTRY,
                provider=provider,
                identity_verified=True,
                evidence={"synthetic_lab": value},
            )
            assert result.verified_for_automation is False
            assert AdapterRegistry().detect(result).name == "generic"


def test_loopback_synthetic_authorization_accepts_only_true_and_never_public_hosts():
    for provider in ("smartrecruiters", "workable"):
        loopback = TargetResolution(
            source_url="http://127.0.0.1:8787/lab/ats/journey",
            final_url="http://127.0.0.1:8787/lab/ats/journey",
            kind=TargetKind.APPLICATION_ENTRY,
            provider=provider,
            identity_verified=True,
            evidence={"synthetic_lab": True},
        )
        assert loopback.verified_for_automation is True
        assert AdapterRegistry().detect(loopback).name == provider
        public = TargetResolution(
            source_url="https://careers.example.test/jobs/journey",
            final_url="https://careers.example.test/jobs/journey",
            kind=TargetKind.APPLICATION_ENTRY,
            provider=provider,
            identity_verified=True,
            evidence={"synthetic_lab": True},
        )
        assert public.verified_for_automation is False
        assert AdapterRegistry().detect(public).name == "generic"
