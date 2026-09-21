"""Employer-alias binding tests for the persisted-target wrong-employer gate.

Live case: opportunity ``b4d46371`` (Chicago Trading Company) carries a
greenhouse board slug ``ctccampusboard`` whose page employer reads
"CTC Campus - External, Not Advertised".  The fixtures below mirror that
live envelope shape so the ONLY failing gate is the employer comparison.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services import navigator
from app.services.navigator import (
    PersistedTargetResolutionError,
    _load_verified_target_from_opportunity,
)

CTC_SOURCE_URL = "https://app.the-trackr.com/uk-finance/summer-internships"
CTC_FINAL_URL = (
    "https://job-boards.greenhouse.io/ctccampusboard/jobs/4709545005"
    "?gh_src=Trackr"
)
CTC_EMPLOYER = "Chicago Trading Company"
CTC_ALIAS = "CTC Campus - External, Not Advertised"
CTC_ROLE = "Quant Trading Internship - Summer 2027"
CTC_ALIAS_BINDING = (
    "greenhouse",
    "ctccampusboard",
    "chicago trading company",
    "ctc campus external not advertised",
)


def _ctc_evidence(employer: str = CTC_ALIAS) -> dict:
    return {
        "application_origin": "https://job-boards.greenhouse.io",
        "destination_origin_bound": True,
        "employer": employer,
        "employer_corroborated": False,
        "form": {
            "binding_verified": True,
            "bound_form_identity": "4709545005",
            "bound_provider": "greenhouse",
            "bound_requisition": "4709545005",
            "bound_role": CTC_ROLE,
            "bound_target_url": CTC_FINAL_URL,
            "control_count": 98,
            "form_identity": "4709545005",
            "frame_url": CTC_FINAL_URL,
            "root_selector": '[data-argus-form-root="202c7fcffdde4e44b59ac03ae12ef23d"]',
            "root_token": "4709545005",
            "submit_present": True,
        },
        "form_identity": "4709545005",
        "inspection_provenance": "application_url",
        "inspection_url": CTC_FINAL_URL,
        "job_identity": {
            "canonical_key": "greenhouse\x1fctccampusboard\x1f4709545005",
            "provenance": "stored_inspection_url",
            "provider": "greenhouse",
            "tenant": "ctccampusboard",
            "vendor_job_id": "4709545005",
        },
        "observed_requisition": "4709545005",
        "provider": "greenhouse",
        "requisition": "4709545005",
        "requisition_match": True,
        "role": CTC_ROLE,
        "role_corroborated": True,
        "source_inspection": True,
    }


def _ctc_opportunity(employer: str = CTC_EMPLOYER) -> SimpleNamespace:
    stored = {
        "source_url": CTC_SOURCE_URL,
        "final_url": CTC_FINAL_URL,
        "kind": "APPLICATION_FORM",
        "provider": "greenhouse",
        "identity_verified": True,
        "form_verified": True,
        "evidence": _ctc_evidence(),
        "verified_target": {
            "application_url": CTC_FINAL_URL,
            "resolved_at": "2026-09-18T13:51:24.284917+00:00",
            "resolved_ats_type": "greenhouse",
            "target_status": "APPLICATION_FORM",
        },
    }
    return SimpleNamespace(
        id="b4d46371-6fd6-4c32-acf9-c8babe857cd0",
        employer=employer,
        role_title=CTC_ROLE,
        url=CTC_SOURCE_URL,
        application_url=CTC_FINAL_URL,
        target_status="APPLICATION_FORM",
        resolved_ats_type="greenhouse",
        resolution_evidence_json=json.dumps(stored),
        resolved_at="2026-09-18T13:51:24",
        navigation_url=CTC_SOURCE_URL,
        automation_url=CTC_FINAL_URL,
    )


def _other_firm_opportunity() -> SimpleNamespace:
    """Same CTC page alias, but bound to a genuinely different firm/board."""
    final_url = "https://job-boards.greenhouse.io/janestreet/jobs/1234567"
    stored = {
        "source_url": "https://aggregator.example.test/jobs/jane-street",
        "final_url": final_url,
        "kind": "APPLICATION_FORM",
        "provider": "greenhouse",
        "identity_verified": True,
        "form_verified": True,
        "evidence": {
            "application_origin": "https://job-boards.greenhouse.io",
            "destination_origin_bound": True,
            # The CTC campus alias page does NOT belong to Jane Street.
            "employer": CTC_ALIAS,
            "employer_corroborated": False,
            "form": {
                "binding_verified": True,
                "bound_form_identity": "1234567",
                "bound_provider": "greenhouse",
                "bound_requisition": "1234567",
                "bound_role": "Quant Trader Intern",
                "bound_target_url": final_url,
                "control_count": 5,
                "form_identity": "1234567",
                "frame_url": final_url,
                "root_selector": '[data-argus-form-root="abc"]',
                "root_token": "1234567",
                "submit_present": True,
            },
            "form_identity": "1234567",
            "inspection_provenance": "application_url",
            "inspection_url": final_url,
            "job_identity": {
                "canonical_key": "greenhouse\x1fjanestreet\x1f1234567",
                "provenance": "stored_inspection_url",
                "provider": "greenhouse",
                "tenant": "janestreet",
                "vendor_job_id": "1234567",
            },
            "observed_requisition": "1234567",
            "provider": "greenhouse",
            "requisition": "1234567",
            "requisition_match": True,
            "role": "Quant Trader Intern",
            "role_corroborated": True,
            "source_inspection": True,
        },
        "verified_target": {
            "application_url": final_url,
            "resolved_at": "2026-09-18T13:51:24.284917+00:00",
            "resolved_ats_type": "greenhouse",
            "target_status": "APPLICATION_FORM",
        },
    }
    return SimpleNamespace(
        id="00000000-0000-4000-8000-000000000001",
        employer="Jane Street",
        role_title="Quant Trader Intern",
        url="https://aggregator.example.test/jobs/jane-street",
        application_url=final_url,
        target_status="APPLICATION_FORM",
        resolved_ats_type="greenhouse",
        resolution_evidence_json=json.dumps(stored),
        resolved_at="2026-09-18T13:51:24",
        navigation_url="https://aggregator.example.test/jobs/jane-street",
        automation_url=final_url,
    )


def test_ctc_alias_passes_once_explicitly_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        navigator, "EMPLOYER_ALIAS_BINDINGS", frozenset({CTC_ALIAS_BINDING})
    )
    resolution = _load_verified_target_from_opportunity(_ctc_opportunity())
    assert resolution is not None


def test_genuinely_different_employer_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even with the CTC alias recorded, a different firm on a different
    # board slug must still fail closed: the binding is scoped to the exact
    # (provider, tenant, employer) triple.
    monkeypatch.setattr(
        navigator, "EMPLOYER_ALIAS_BINDINGS", frozenset({CTC_ALIAS_BINDING})
    )
    with pytest.raises(PersistedTargetResolutionError, match="employer"):
        _load_verified_target_from_opportunity(_other_firm_opportunity())


def test_unrecorded_alias_fails_closed() -> None:
    # The production table is empty until a human confirms each alias, so
    # the live CTC envelope must still fail closed by default.
    assert navigator.EMPLOYER_ALIAS_BINDINGS == frozenset()
    with pytest.raises(PersistedTargetResolutionError, match="employer"):
        _load_verified_target_from_opportunity(_ctc_opportunity())
