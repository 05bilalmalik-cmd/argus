"""Tests for vendor detectors behind custom domains.

These tests verify that ARGUS can positively identify known ATS vendors
served from custom/vanity domains, using STRONG evidence (a combination
of signals from different categories), without widening the egress/trust
allowlist.

Vendors tested (in order of unlock potential from live DB measurement):
1. TalentLink (tal.net) - 41 rows
2. Oracle HCM (oraclecloud.com) - 19 rows
3. Cornerstone (csod.com) - 9 rows
4. TalentView (talentview.io) - 8 rows
5. Recruitee (recruitee.com) - 8 rows
6. Avature (avature.net) - 4 rows
7. Breezy (breezy.hr) - 5 rows
8. iCIMS (icims.com) - 3 rows
"""
from __future__ import annotations

from app.automation.targets import (
    classify_target,
    trusted_provider_for_url,
    _detect_greenhouse_custom_domain,
    _detect_talentlink_custom_domain,
    _detect_oracle_hcm_custom_domain,
    _detect_cornerstone_custom_domain,
    _detect_talentview_custom_domain,
    _detect_recruitee_custom_domain,
    _detect_avature_custom_domain,
    _detect_breezy_custom_domain,
    _detect_icims_custom_domain,
)
from app.domain.targets import TargetKind
from app.automation.host_policy import FIRST_PARTY_SERVICE_MANIFEST


# ======================================================================
# TalentLink (tal.net) - 41 rows unlock potential
# ======================================================================

class TestTalentLinkCustomDomainDetection:
    """Tests for _detect_talentlink_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.tal.net host URL + TalentLink iframe = positive detection (2 categories)."""
        url = "https://rothschildandco.tal.net/jobs/123"
        html = '''
        <html>
            <body>
                <iframe src="https://rothschildandco.tal.net/embed/job/123"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_talentlink_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["talentlink_custom_domain_signals"]["talentlink_host_url"] is True
        assert result["talentlink_custom_domain_signals"]["talentlink_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to tal.net + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class. A hostile page can manufacture both without ever touching the
        vendor, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.tal.net/vacancy/456">Apply</a>
                <div data-tal-requisition-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_talentlink_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_form_action_plus_requisition_is_detected(self) -> None:
        """Form action to tal.net + vendor requisition in DOM = positive detection.

        vendor_endpoint (form to a tal.net host) + body (vendor-specific
        tal-requisition marker) = two DIFFERENT provenance classes.
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <form action="https://company.tal.net/apply/789"></form>
                <meta name="tal-requisition" content="789">
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_talentlink_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["talentlink_custom_domain_signals"]["talentlink_form_action"] is True
        assert result["talentlink_custom_domain_signals"]["talentlink_requisition"] is True

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.tal.net host URL alone is NOT enough (1 category only)."""
        url = "https://company.tal.net/jobs/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_talentlink_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'talentlink' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use talentlink for hiring</body></html>"
        evidence = {}
        result = _detect_talentlink_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"

    def test_single_weak_marker_iframe_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: iframe to tal.net alone is NOT enough (1 category)."""
        url = "https://www.random-site.com/jobs/123"
        html = '<html><body><iframe src="https://company.tal.net/embed/job/123"></iframe></body></html>'
        evidence = {}
        result = _detect_talentlink_custom_domain(url, html, evidence)
        assert result is None, "Single category (iframe only) should NOT trigger detection"


# ======================================================================
# Oracle HCM (oraclecloud.com) - 19 rows unlock potential
# ======================================================================

class TestOracleHCMCustomDomainDetection:
    """Tests for _detect_oracle_hcm_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.oraclecloud.com host URL + Oracle iframe = positive detection (2 categories)."""
        url = "https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/123"
        html = '''
        <html>
            <body>
                <iframe src="https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/123/iframe"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_oracle_hcm_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["oracle_hcm_custom_domain_signals"]["oracle_host_url"] is True
        assert result["oracle_hcm_custom_domain_signals"]["oracle_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to oraclecloud.com + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/456">Apply</a>
                <div data-oracle-requisition-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_oracle_hcm_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_form_action_plus_requisition_is_detected(self) -> None:
        """Form action to oraclecloud.com + vendor requisition in DOM = positive.

        vendor_endpoint (form to an oraclecloud.com host) + body
        (vendor-specific oracle-requisition marker) = two DIFFERENT classes.
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <form action="https://company.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/789/apply"></form>
                <meta name="oracle-requisition" content="789">
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_oracle_hcm_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["oracle_hcm_custom_domain_signals"]["oracle_form_action"] is True
        assert result["oracle_hcm_custom_domain_signals"]["oracle_requisition"] is True

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.oraclecloud.com host URL alone is NOT enough (1 category only)."""
        url = "https://company.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_oracle_hcm_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'oracle' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use oracle for hiring</body></html>"
        evidence = {}
        result = _detect_oracle_hcm_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# Cornerstone (csod.com) - 9 rows unlock potential
# ======================================================================

class TestCornerstoneCustomDomainDetection:
    """Tests for _detect_cornerstone_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.csod.com host URL + Cornerstone iframe = positive detection (2 categories)."""
        url = "https://eurazeo.csod.com/ats/careersite/JobDetails.aspx?id=123"
        html = '''
        <html>
            <body>
                <iframe src="https://eurazeo.csod.com/ats/careersite/JobDetailsFrame.aspx?id=123"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_cornerstone_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["cornerstone_custom_domain_signals"]["cornerstone_host_url"] is True
        assert result["cornerstone_custom_domain_signals"]["cornerstone_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to csod.com + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.csod.com/ats/careersite/JobDetails.aspx?id=456">Apply</a>
                <div data-csod-job-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_cornerstone_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.csod.com host URL alone is NOT enough (1 category only)."""
        url = "https://company.csod.com/ats/careersite/JobDetails.aspx?id=123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_cornerstone_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'cornerstone' or 'csod' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use cornerstone csod for hiring</body></html>"
        evidence = {}
        result = _detect_cornerstone_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# TalentView (talentview.io) - 8 rows unlock potential
# ======================================================================

class TestTalentViewCustomDomainDetection:
    """Tests for _detect_talentview_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.talentview.io host URL + TalentView iframe = positive detection (2 categories)."""
        url = "https://tikehau-capital-career.talentview.io/jobs/123"
        html = '''
        <html>
            <body>
                <iframe src="https://tikehau-capital-career.talentview.io/embed/123"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_talentview_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["talentview_custom_domain_signals"]["talentview_host_url"] is True
        assert result["talentview_custom_domain_signals"]["talentview_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to talentview.io + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.talentview.io/jobs/456">Apply</a>
                <div data-tv-job-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_talentview_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.talentview.io host URL alone is NOT enough (1 category only)."""
        url = "https://company.talentview.io/jobs/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_talentview_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'talentview' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use talentview for hiring</body></html>"
        evidence = {}
        result = _detect_talentview_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# Recruitee (recruitee.com) - 8 rows unlock potential
# ======================================================================

class TestRecruiteeCustomDomainDetection:
    """Tests for _detect_recruitee_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.recruitee.com host URL + Recruitee iframe = positive detection (2 categories)."""
        url = "https://ikpartners.recruitee.com/o/123"
        html = '''
        <html>
            <body>
                <iframe src="https://ikpartners.recruitee.com/embed/o/123"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_recruitee_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["recruitee_custom_domain_signals"]["recruitee_host_url"] is True
        assert result["recruitee_custom_domain_signals"]["recruitee_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to recruitee.com + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.recruitee.com/o/456">Apply</a>
                <div data-recruitee-offer-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_recruitee_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.recruitee.com host URL alone is NOT enough (1 category only)."""
        url = "https://company.recruitee.com/o/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_recruitee_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'recruitee' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use recruitee for hiring</body></html>"
        evidence = {}
        result = _detect_recruitee_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# Avature (avature.net) - 4 rows unlock potential
# ======================================================================

class TestAvatureCustomDomainDetection:
    """Tests for _detect_avature_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.avature.net host URL + Avature iframe = positive detection (2 categories)."""
        url = "https://carlyle.avature.net/careers/JobDetail/123"
        html = '''
        <html>
            <body>
                <iframe src="https://carlyle.avature.net/careers/JobDetailFrame/123"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_avature_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["avature_custom_domain_signals"]["avature_host_url"] is True
        assert result["avature_custom_domain_signals"]["avature_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to avature.net + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.avature.net/careers/JobDetail/456">Apply</a>
                <div data-avature-job-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_avature_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.avature.net host URL alone is NOT enough (1 category only)."""
        url = "https://company.avature.net/careers/JobDetail/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_avature_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'avature' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use avature for hiring</body></html>"
        evidence = {}
        result = _detect_avature_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# Breezy (breezy.hr) - 5 rows unlock potential
# ======================================================================

class TestBreezyCustomDomainDetection:
    """Tests for _detect_breezy_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.breezy.hr host URL + Breezy iframe = positive detection (2 categories)."""
        url = "https://t-capital.breezy.hr/position/123"
        html = '''
        <html>
            <body>
                <iframe src="https://t-capital.breezy.hr/embed/position/123"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_breezy_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["breezy_custom_domain_signals"]["breezy_host_url"] is True
        assert result["breezy_custom_domain_signals"]["breezy_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to breezy.hr + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.breezy.hr/position/456">Apply</a>
                <div data-breezy-position-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_breezy_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.breezy.hr host URL alone is NOT enough (1 category only)."""
        url = "https://company.breezy.hr/position/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_breezy_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'breezy' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use breezy for hiring</body></html>"
        evidence = {}
        result = _detect_breezy_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# iCIMS (icims.com) - 3 rows unlock potential
# ======================================================================

class TestICIMSCustomDomainDetection:
    """Tests for _detect_icims_custom_domain function."""

    def test_host_url_plus_iframe_is_detected(self) -> None:
        """*.icims.com host URL + iCIMS iframe = positive detection (2 categories)."""
        url = "https://careers-hines.icims.com/jobs/123"
        html = '''
        <html>
            <body>
                <iframe src="https://careers-hines.icims.com/jobs/123/iframe"></iframe>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_icims_custom_domain(url, html, evidence)
        assert result is not None
        assert result["custom_domain_verified"] is True
        assert result["icims_custom_domain_signals"]["icims_host_url"] is True
        assert result["icims_custom_domain_signals"]["icims_iframe"] is True

    def test_link_plus_dom_marker_without_vendor_endpoint_NOT_detected(self) -> None:
        """FAIL-CLOSED: link to icims.com + DOM marker is NOT enough (body + body).

        Both signals are page-controlled content from a single provenance
        class, so detection requires a second class (url or vendor_endpoint).
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <a href="https://company.icims.com/jobs/456">Apply</a>
                <div data-icims-job-id="456"></div>
            </body>
        </html>
        '''
        evidence = {}
        result = _detect_icims_custom_domain(url, html, evidence)
        assert result is None, "Two body signals share one provenance class and must NOT trigger detection"

    def test_single_weak_marker_host_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: *.icims.com host URL alone is NOT enough (1 category only)."""
        url = "https://company.icims.com/jobs/123"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_icims_custom_domain(url, html, evidence)
        assert result is None, "Single category (host URL only) should NOT trigger detection"

    def test_single_weak_marker_text_alone_NOT_detected(self) -> None:
        """MANDATORY NEGATIVE: 'icims' in body text alone is NOT enough."""
        url = "https://www.random-site.com/jobs/123"
        html = "<html><body>We use icims for hiring</body></html>"
        evidence = {}
        result = _detect_icims_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (text mention) should NOT trigger detection"


# ======================================================================
# Classification Integration Tests
# ======================================================================

def _make_evidence(provider: str, employer: str, role: str, requisition: str) -> dict:
    """Standard evidence bundle that navigator would provide."""
    return {
        "ats": provider,
        "employer": employer,
        "role": role,
        "requisition_id": requisition,
        "form_id": "application-form",
    }


class TestTalentLinkCustomDomainClassification:
    """Tests for classify_target with TalentLink custom domain evidence."""

    def test_rothschildandco_style_page_matched_to_talentlink_adapter(self) -> None:
        """rothschildandco.tal.net-style page with host URL + iframe IS matched."""
        url = "https://rothschildandco.tal.net/vacancy/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://rothschildandco.tal.net/embed/vacancy/12345"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("talentlink", "Rothschild & Co", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="talentlink",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "talentlink"
        assert resolution.identity_verified is True
        # Path /vacancy/ doesn't match /apply/ pattern, so JOB_DETAIL is correct
        assert resolution.kind == TargetKind.JOB_DETAIL
        assert resolution.evidence.get("custom_domain_verified") is True

    def test_arbitrary_domain_with_single_weak_marker_falls_through(self) -> None:
        """MANDATORY NEGATIVE: Arbitrary domain with only talentlink text falls through."""
        url = "https://www.unrelated-company.com/careers/123"
        html = "<html><body>We use talentlink for hiring</body></html>"
        evidence = {}
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="talentlink",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.kind == TargetKind.UNRESOLVED
        assert "untrusted_provider_hint" in resolution.reason_codes


class TestOracleHCMCustomDomainClassification:
    """Tests for classify_target with Oracle HCM custom domain evidence."""

    def test_natixis_style_page_matched_to_oracle_adapter(self) -> None:
        """ekez.fa.em2.oraclecloud.com-style page with host URL + iframe IS matched."""
        url = "https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/12345/iframe"></iframe>
                <meta name="requisition" content="12345">
            </body>
        </html>
        '''
        evidence = _make_evidence("oracle", "Natixis", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="oracle",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "oracle"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True

    def test_arbitrary_domain_with_single_weak_marker_falls_through(self) -> None:
        """MANDATORY NEGATIVE: Arbitrary domain with only oracle text falls through."""
        url = "https://www.unrelated-company.com/careers/123"
        html = "<html><body>We use oracle for hiring</body></html>"
        evidence = {}
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="oracle",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.kind == TargetKind.UNRESOLVED
        assert "untrusted_provider_hint" in resolution.reason_codes


class TestCornerstoneCustomDomainClassification:
    """Tests for classify_target with Cornerstone custom domain evidence."""

    def test_eurazeo_style_page_matched_to_cornerstone_adapter(self) -> None:
        """eurazeo.csod.com-style page with host URL + iframe IS matched."""
        url = "https://eurazeo.csod.com/ats/careersite/JobDetails.aspx?id=12345"
        html = '''
        <html>
            <body>
                <iframe src="https://eurazeo.csod.com/ats/careersite/JobDetailsFrame.aspx?id=12345"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("cornerstone", "Eurazeo", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="cornerstone",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "cornerstone"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True


class TestTalentViewCustomDomainClassification:
    """Tests for classify_target with TalentView custom domain evidence."""

    def test_tikehau_style_page_matched_to_talentview_adapter(self) -> None:
        """tikehau-capital-career.talentview.io-style page with host URL + iframe IS matched."""
        url = "https://tikehau-capital-career.talentview.io/jobs/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://tikehau-capital-career.talentview.io/embed/12345"></iframe>
                <div data-tv-job-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("talentview", "Tikehau Capital", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="talentview",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "talentview"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True


class TestRecruiteeCustomDomainClassification:
    """Tests for classify_target with Recruitee custom domain evidence."""

    def test_ikpartners_style_page_matched_to_recruitee_adapter(self) -> None:
        """ikpartners.recruitee.com-style page with host URL + iframe IS matched."""
        url = "https://ikpartners.recruitee.com/o/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://ikpartners.recruitee.com/embed/o/12345"></iframe>
                <div data-offer-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("recruitee", "IK Partners", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="recruitee",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "recruitee"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True


class TestAvatureCustomDomainClassification:
    """Tests for classify_target with Avature custom domain evidence."""

    def test_carlyle_style_page_matched_to_avature_adapter(self) -> None:
        """carlyle.avature.net-style page with host URL + iframe IS matched."""
        url = "https://carlyle.avature.net/careers/JobDetail/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://carlyle.avature.net/careers/JobDetailFrame/12345"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("avature", "The Carlyle Group", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="avature",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "avature"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True


class TestBreezyCustomDomainClassification:
    """Tests for classify_target with Breezy custom domain evidence."""

    def test_tcapital_style_page_matched_to_breezy_adapter(self) -> None:
        """t-capital.breezy.hr-style page with host URL + iframe IS matched."""
        url = "https://t-capital.breezy.hr/position/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://t-capital.breezy.hr/embed/position/12345"></iframe>
                <div data-position-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("breezy", "T.Capital", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="breezy",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "breezy"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True


class TestICIMSCustomDomainClassification:
    """Tests for classify_target with iCIMS custom domain evidence."""

    def test_hines_style_page_matched_to_icims_adapter(self) -> None:
        """careers-hines.icims.com-style page with host URL + iframe IS matched."""
        url = "https://careers-hines.icims.com/jobs/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://careers-hines.icims.com/jobs/12345/iframe"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("icims", "Hines", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="icims",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "icims"
        assert resolution.identity_verified is True
        assert resolution.evidence.get("custom_domain_verified") is True


# ======================================================================
# Security Tests: Egress/Trust Allowlist NOT Widened
# ======================================================================

class TestCustomDomainDetectionDoesNotWidenEgressAllowlist:
    """MANDATORY SECURITY: Matching does NOT add custom host to egress/trust allowlist."""

    def test_talentlink_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://rothschildandco.tal.net/vacancy/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://rothschildandco.tal.net/embed/vacancy/12345"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("talentlink", "Rothschild & Co", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="talentlink",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "talentlink"
        assert resolution.identity_verified is True

        # Verify the host policy manifest is UNCHANGED
        # (TalentLink may not have a manifest entry; the key assertion is that
        # trusted_provider_for_url still returns empty for the custom domain)
        assert trusted_provider_for_url(url) == ""

    def test_oracle_hcm_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://ekez.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/12345/iframe"></iframe>
                <meta name="requisition" content="12345">
            </body>
        </html>
        '''
        evidence = _make_evidence("oracle", "Natixis", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="oracle",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "oracle"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""

    def test_cornerstone_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://eurazeo.csod.com/ats/careersite/JobDetails.aspx?id=12345"
        html = '''
        <html>
            <body>
                <iframe src="https://eurazeo.csod.com/ats/careersite/JobDetailsFrame.aspx?id=12345"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("cornerstone", "Eurazeo", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="cornerstone",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "cornerstone"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""

    def test_talentview_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://tikehau-capital-career.talentview.io/jobs/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://tikehau-capital-career.talentview.io/embed/12345"></iframe>
                <div data-tv-job-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("talentview", "Tikehau Capital", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="talentview",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "talentview"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""

    def test_recruitee_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://ikpartners.recruitee.com/o/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://ikpartners.recruitee.com/embed/o/12345"></iframe>
                <div data-offer-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("recruitee", "IK Partners", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="recruitee",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "recruitee"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""

    def test_avature_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://carlyle.avature.net/careers/JobDetail/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://carlyle.avature.net/careers/JobDetailFrame/12345"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("avature", "The Carlyle Group", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="avature",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "avature"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""

    def test_breezy_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://t-capital.breezy.hr/position/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://t-capital.breezy.hr/embed/position/12345"></iframe>
                <div data-position-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("breezy", "T.Capital", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="breezy",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "breezy"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""

    def test_icims_detection_does_not_add_host_to_allowlist(self) -> None:
        url = "https://careers-hines.icims.com/jobs/12345"
        html = '''
        <html>
            <body>
                <iframe src="https://careers-hines.icims.com/jobs/12345/iframe"></iframe>
                <div data-requisition-id="12345"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("icims", "Hines", "Analyst", "12345")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="icims",
            identity_verified=True,
            evidence=evidence,
        )
        assert resolution.provider == "icims"
        assert resolution.identity_verified is True
        assert trusted_provider_for_url(url) == ""


# ======================================================================
# Ambiguity Test: Two Vendors' Signals = Fail Closed
# ======================================================================

class TestAmbiguousSignalsFailClosed:
    """MANDATORY: Page with signals from TWO different vendors must fail closed."""

    def test_talentlink_and_oracle_signals_together_fails_closed(self) -> None:
        """FAIL-CLOSED: page with both TalentLink and Oracle proof is ambiguous.

        TalentLink fires (iframe to tal.net + data-tal- marker: vendor_endpoint
        + body) AND Oracle fires (form to oraclecloud.com + data-oracle-
        marker: vendor_endpoint + body). Two vendors claiming one page means
        the resolution must be UNRESOLVED/MISMATCH with NO provider claim.
        First-match-wins would be a security defect, not a tie-break.
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <iframe src="https://company.tal.net/embed/123"></iframe>
                <div data-tal-requisition="123"></div>
                <form action="https://company.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/456/apply"></form>
                <a href="https://company.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/456">Also Oracle</a>
                <div data-oracle-requisition="456"></div>
            </body>
        </html>
        '''
        # Provide evidence for TalentLink
        evidence = _make_evidence("talentlink", "Example Corp", "Analyst", "123")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="talentlink",
            identity_verified=True,
            evidence=evidence,
        )
        # Ambiguous: must fail closed with no vendor provider claim.
        assert resolution.provider == ""
        assert resolution.kind in (TargetKind.UNRESOLVED, TargetKind.MISMATCH)
        assert "conflicting_custom_domain_providers" in resolution.reason_codes

    def test_greenhouse_and_talentlink_signals_together_fails_closed(self) -> None:
        """FAIL-CLOSED: page with both Greenhouse and TalentLink proof is ambiguous.

        Greenhouse fires (iframe to boards.greenhouse.io + data-org marker)
        AND TalentLink fires (iframe to tal.net + data-tal- marker). The old
        test asserted "greenhouse wins" by detector ordering; that asserted a
        vulnerability. Ambiguity must resolve to UNRESOLVED/MISMATCH with NO
        provider claim.
        """
        url = "https://careers.example.com/job/123"
        html = '''
        <html>
            <body>
                <iframe src="https://boards.greenhouse.io/embed/job_app?for=test&token=123"></iframe>
                <div data-org="test"></div>
                <iframe src="https://company.tal.net/embed/123"></iframe>
                <div data-tal-requisition="123"></div>
            </body>
        </html>
        '''
        evidence = _make_evidence("greenhouse", "Example Corp", "Analyst", "123")
        resolution = classify_target(
            source_url=url,
            final_url=url,
            html=html,
            provider_hint="greenhouse",
            identity_verified=True,
            evidence=evidence,
        )
        # Ambiguous: must fail closed with no vendor provider claim.
        assert resolution.provider == ""
        assert resolution.kind in (TargetKind.UNRESOLVED, TargetKind.MISMATCH)
        assert "conflicting_custom_domain_providers" in resolution.reason_codes


# ======================================================================
# Greenhouse Regression Guard
# ======================================================================

class TestGreenhouseDetectorRegression:
    """MANDATORY REGRESSION: Existing Greenhouse detector still behaves exactly as before."""

    def test_gh_jid_plus_iframe_still_detected(self) -> None:
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

    def test_single_weak_marker_gh_src_alone_still_NOT_detected(self) -> None:
        url = "https://www.random-site.com/jobs/123?gh_src=abc"
        html = "<html><body>Some page</body></html>"
        evidence = {}
        result = _detect_greenhouse_custom_domain(url, html, evidence)
        assert result is None, "Single weak marker (gh_src) should NOT trigger detection"

    def test_real_greenhouse_io_page_still_matches(self) -> None:
        url = "https://boards.greenhouse.io/jumptrading/jobs/456789"
        html = '<html><body><div data-org="jumptrading"></div></body></html>'
        evidence = _make_evidence("greenhouse", "Jump Trading", "Quant Researcher", "456789")
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


# ======================================================================
# Evidence Threshold Documentation Tests
# ======================================================================

class TestTalentLinkEvidenceThreshold:
    """Document the exact evidence threshold for TalentLink."""

    def test_two_categories_required(self) -> None:
        # Category A: host URL
        url = "https://company.tal.net/job?tal_id=123"
        html = "<html><body></body></html>"
        assert _detect_talentlink_custom_domain(url, html, {}) is None

        # 2 categories (A + B) -> detected
        html = '<html><body><iframe src="https://company.tal.net/embed/123"></iframe></body></html>'
        assert _detect_talentlink_custom_domain(url, html, {}) is not None

        # 2 categories (A + C) -> detected (vendor-specific body marker)
        html = '<html><body><div data-tal-requisition-id="123"></div></body></html>'
        assert _detect_talentlink_custom_domain(url, html, {}) is not None


class TestOracleHCMEvidenceThreshold:
    """Document the exact evidence threshold for Oracle HCM."""

    def test_two_categories_required(self) -> None:
        url = "https://company.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions/123"
        html = "<html><body></body></html>"
        assert _detect_oracle_hcm_custom_domain(url, html, {}) is None

        html = '<html><body><iframe src="https://company.fa.oraclecloud.com/hcmUI/.../iframe"></iframe></body></html>'
        assert _detect_oracle_hcm_custom_domain(url, html, {}) is not None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])