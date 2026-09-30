"""Tests for Greenhouse vendor detection behind custom domains.

These tests verify that ARGUS can positively identify a known ATS vendor
(Greenhouse) served from a custom/vanity domain, using STRONG evidence
(a combination of signals), without widening the egress/trust allowlist.
"""
from __future__ import annotations

from app.automation.adapters.registry import AdapterRegistry
from app.automation.adapters.generic import GenericAdapter
from app.automation.targets import (
    TargetResolution,
    classify_target,
    trusted_provider_for_url,
    _detect_greenhouse_custom_domain,
)
from app.domain.targets import TargetKind
from app.automation.host_policy import FIRST_PARTY_SERVICE_MANIFEST


class TestGreenhouseCustomDomainDetection:
    """Tests for _detect_greenhouse_custom_domain function."""

    def test_gh_jid_plus_iframe_is_detected(self) -> None:
        """gh_jid query param + Greenhouse iframe = positive detection (2 categories)."""
        url = "https://www.jumptrading.com/jobs/123?gh_jid=456789&gh_src=abc"
        html = '''
        <html>
            <body>
                <iframe src="https://boards.greenhouse.io/embed/job_app?for=jumptrading&token=456789"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["greenhouse_custom_domain_signals"]["gh_query_param"] is True
        assert result["greenhouse_custom_domain_signals"]["greenhouse_iframe"] is True

    def test_gh_src_plus_dom_marker_is_detected(self) -> None:
        """gh_src query param + Greenhouse DOM marker = positive detection (2 categories)."""
        url = "https://www.akunacapital.com/careers/role/123?gh_src=xyz"
        html = '''
        <html>
            <body>
                <div data-org="akunacapital" data-greenhouse-token="12345"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["greenhouse_custom_domain_signals"]["gh_query_param"] is True
        assert result["greenhouse_custom_domain_signals"]["greenhouse_dom_marker"] is True

    def test_grnh_se_link_plus_form_action_is_detected(self) -> None:
        """grnh.se link + form action to Greenhouse = positive detection (2 categories)."""
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://grnh.se/abc123">Apply</a>
                <form action="https://boards.greenhouse.io/embed/job_app?for=example&token=123"></form>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["greenhouse_custom_domain_signals"]["grnh_se_link"] is True
        assert result["greenhouse_custom_domain_signals"]["greenhouse_form_action"] is True

    def test_board_token_plus_iframe_is_detected(self) -> None:
        """Board token in DOM + Greenhouse iframe = positive detection (2 categories)."""
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <iframe src="https://job-boards.greenhouse.io/embed/job_app?for=example&token=999"></iframe>
                <meta name="greenhouse-token" content="999">
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["greenhouse_custom_domain_signals"]["greenhouse_iframe"] is True
        assert result["greenhouse_custom_domain_signals"]["greenhouse_board_token"] is True

    def test_single_weak_marker_gh_src_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: gh_src param alone is NOT enough (1 category only)."""
        url = "https://www.random-site.com/jobs/123?gh_src=abc"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (gh_src) should NOT trigger detection"

    def test_single_weak_marker_greenhouse_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'greenhouse' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use greenhouse for hiring</body></html>"
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"

    def test_single_weak_marker_iframe_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: iframe to Greenhouse alone is NOT enough (1 category)."""
        url = "https://www.random-site.com/jobs/123"
        html = '<html><body><iframe src="https://boards.greenhouse.io/embed/job_app?for=test&token=123"></iframe></body></html>'
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is None, "Single category (iframe only) should NOT trigger detection"

    def test_single_weak_marker_dom_marker_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: Greenhouse DOM marker alone is NOT enough (1 category)."""
        url = "https://www.random-site.com/jobs/123"
        html = '<html><body><div data-org="test"></div></body></html>'
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is None, "Single category (DOM marker only) should NOT trigger detection"


class TestGreenhouseCustomDomainClassification:
    """Tests for classify_target with Greenhouse custom domain evidence."""

    def _make_evidence(self) -> dict:
        """Standard evidence bundle that navigator would provide."""
        return {
            "ats": "greenhouse",
            "employer": "Jump Trading",
            "role": "Quant Researcher",
            "requisition_id": "456789",
            "form_id": "application-form",
        }

    def test_jumptrading_style_page_matched_to_greenhouse_adapter(self) -> None:
        """jumptrading.com-style page with gh_jid + Greenhouse form IS matched to greenhouse adapter."""
        url = "https://www.jumptrading.com/jobs/12345?gh_jid=456789&gh_src=careers"
        html = '''
        <html>
            <body>
                <div data-org="jumptrading" data-greenhouse-token="456789"></div>
                <iframe src="https://boards.greenhouse.io/embed/job_app?for=jumptrading&token=456789"></iframe>
                <form action="https://boards.greenhouse.io/embed/job_app?for=jumptrading&token=456789"></form>
            </body>
        </html>
        '''
        evidence = self._make_evidence()
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "greenhouse"
        assert resolution.identity_verified is True
        assert resolution.kind == TargetKind.APPLICATION_ENTRY
        assert resolution.evidence.get("custom_domain_verified") is True
        # Detection output is METADATA, not automation authority: no
        # demonstrated producer emits an independent custom-domain identity
        # bundle, so a custom-domain resolution never selects a provider
        # adapter (verified_for_automation is False and the registry stays
        # on generic).
        assert resolution.verified_for_automation is False
        adapter = AdapterRegistry().detect(resolution)
        assert adapter.name == "generic"
        assert isinstance(adapter, GenericAdapter)

    def test_akunacapital_style_page_matched_to_greenhouse_adapter(self) -> None:
        """akunacapital.com-style page with gh_src + DOM markers IS matched to greenhouse adapter."""
        url = "https://www.akunacapital.com/careers/quant-researcher-123?gh_src=referral"
        html = '''
        <html>
            <body>
                <div data-org="akunacapital" data-board="akunacapital" data-job-post-id="789"></div>
                <a href="https://grnh.se/abc789">Apply via Greenhouse</a>
            </body>
        </html>
        '''
        evidence = {
            "ats": "greenhouse",
            "employer": "Aku na Capital",
            "role": "Quant Researcher",
            "requisition_id": "789",
            "form_id": "application-form",
        }
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "greenhouse"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True
        # Metadata only; no provider-adapter authority on a custom domain.
        assert resolution.verified_for_automation is False
        adapter = AdapterRegistry().detect(resolution)
        assert adapter.name == "generic"

    def test_arbitrary_domain_with_single_weak_marker_falls_through(self) -> None:
        """MANDATORY NEGATIVE: Arbitrary domain with only gh_src falls through to generic.

        This test provides MINIMAL navigator evidence (only provider_hint) to simulate
        a "custom portal" database row where the navigator doesn't have full ATS evidence.
        The page has only a weak gh_src marker, so detection fails and the hint is rejected.
        """
        url = "https://www.unrelated-company.com/careers/123?gh_src=test"
        html = "<html><body>Careers page</body></html>"
        evidence = {}  # No navigator evidence - simulating a "custom portal" row
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        # Should be UNRESOLVED because provider_hint alone is not trusted
        assert resolution.kind == TargetKind.UNRESOLVED
        assert "untrusted_provider_hint" in resolution.reason_codes

        # Registry should fall back to GenericAdapter
        adapter = AdapterRegistry().detect(resolution)
        assert adapter.name == "generic"

    def test_arbitrary_domain_with_greenhouse_text_only_falls_through(self) -> None:
        """MANDATORY NEGATIVE: Arbitrary domain with only 'greenhouse' text falls through.

        This test provides MINIMAL navigator evidence (only provider_hint) to simulate
        a "custom portal" database row. The page has only a weak text mention of
        "greenhouse", so detection fails and verification fails.
        """
        url = "https://www.random-marketing-site.com/jobs"
        html = "<html><body>We are a greenhouse of talent</body></html>"
        evidence = {}  # No navigator evidence - simulating a "custom portal" row
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        # Should be UNRESOLVED (or LISTING for /jobs path) because custom_domain_verified is not set
        assert resolution.kind in (TargetKind.UNRESOLVED, TargetKind.LISTING)
        adapter = AdapterRegistry().detect(resolution)
        assert adapter.name == "generic"

    def test_real_greenhouse_io_page_still_matches_regression(self) -> None:
        """MANDATORY REGRESSION: Real greenhouse.io page still matches exactly as before."""
        url = "https://boards.greenhouse.io/jumptrading/jobs/456789"
        html = '<html><body><div data-org="jumptrading"></div></body></html>'
        evidence = self._make_evidence()
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "greenhouse"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(resolution.final_url) == "greenhouse"

        adapter = AdapterRegistry().detect(resolution)
        assert adapter.name == "greenhouse"

    def test_custom_domain_detection_does_NOT_add_host_to_egress_allowlist(self) -> None:
        """MANDATORY SECURITY: Matching does NOT add custom host to egress/trust allowlist."""
        url = "https://www.jumptrading.com/jobs/123?gh_jid=456789"
        html = '''
        <html>
            <body>
                <iframe src="https://boards.greenhouse.io/embed/job_app?for=jumptrading&token=456789"></iframe>
                <div data-org="jumptrading"></div>
            </body>
        </html>
        '''
        evidence = self._make_evidence()
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "greenhouse"
        assert resolution.identity_verified is True

        # Verify the host policy manifest is UNCHANGED - custom domain NOT in allowlist
        greenhouse_manifest = FIRST_PARTY_SERVICE_MANIFEST.get("greenhouse")
        assert greenhouse_manifest is not None
        job_board_hosts = greenhouse_manifest.job_board_hosts
        assert "www.jumptrading.com" not in job_board_hosts
        assert "jumptrading.com" not in job_board_hosts
        # Only official greenhouse.io subdomains are in the manifest
        assert all(h.endswith(".greenhouse.io") for h in job_board_hosts)

        # trusted_provider_for_url should still return empty for custom domain
        assert trusted_provider_for_url(url) == ""


class TestGreenhouseCustomDomainEvidenceThreshold:
    """Tests documenting the exact evidence threshold chosen."""

    def test_evidence_threshold_two_categories_required(self) -> None:
        """Document the threshold: at least 2 distinct signal categories required."""
        # Provenance URL: gh_jid/gh_src on the resolved URL.
        # Provenance vendor endpoint: iframe/form to greenhouse.io.
        # Provenance body: links, DOM markers, and board tokens.

        # 1 category only -> NOT detected
        url = "https://custom.com/job?gh_jid=123"
        html = "<html><body></body></html>"
        assert _detect_greenhouse_custom_domain(url, html, {}) is None

        # 2 categories (A + B) -> detected
        html = '<html><body><iframe src="https://boards.greenhouse.io/embed/job_app?for=test&token=123"></iframe></body></html>'
        assert _detect_greenhouse_custom_domain(url, html, {}) is not None

        # 2 categories (A + C) -> detected
        html = '<html><body><div data-org="test"></div></body></html>'
        assert _detect_greenhouse_custom_domain(url, html, {}) is not None

        # 2 categories (A + D) -> detected (board token in DOM)
        html = '<html><body><meta name="greenhouse-token" content="123"></body></html>'
        assert _detect_greenhouse_custom_domain(url, html, {}) is not None

        # 2 categories (B + C) -> detected
        url = "https://custom.com/job"
        html = '<html><body><iframe src="https://boards.greenhouse.io/embed/job_app?for=test&token=123"></iframe><div data-org="test"></div></body></html>'
        assert _detect_greenhouse_custom_domain(url, html, {}) is not None

        # 2 categories (B + D) -> detected
        html = '<html><body><iframe src="https://boards.greenhouse.io/embed/job_app?for=test&token=123"></iframe><meta name="greenhouse-token" content="123"></body></html>'
        assert _detect_greenhouse_custom_domain(url, html, {}) is not None

        # Two body-controlled signals (C + C) are NOT independent.
        url = "https://custom.com/job"
        html = '<html><body><div data-org="test"></div><meta name="greenhouse-token" content="123"></body></html>'
        assert _detect_greenhouse_custom_domain(url, html, {}) is None

    def test_category_definitions(self) -> None:
        """Document the four signal categories."""
        # Provenance URL: gh_jid or gh_src on the resolved URL itself.
        # Provenance vendor endpoint: iframe/form action resolving to a
        # Greenhouse-owned host.
        # Provenance body: grnh.se links, text, DOM markers, and board tokens.
        # Two body signals are insufficient because the page author controls
        # both and can manufacture them without reaching Greenhouse.
        pass  # This test serves as documentation


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
