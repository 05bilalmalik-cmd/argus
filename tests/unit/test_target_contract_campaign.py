"""Target proof/promotion contract campaign (RED then GREEN).

Owns only the shared contract + service binding boundary. No navigator,
runner, registry, or allowlist changes. Synthetic loopback/temp DB only.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.automation.targets import (
    TargetResolution,
    validate_target_resolution_contract,
)
from app.automation.host_policy import origin_for_url
from app.db import Base
from app.domain.targets import TargetKind
from app.models import Opportunity
from app.services.navigator import _load_verified_target_from_opportunity
from app.services.target_resolution import TargetResolutionService

GH = "https://boards.greenhouse.io/acme/jobs/123456"
CUSTOM = "https://careers.attacker.test/jobs/123456?gh_jid="
LOOPBACK = "http://127.0.0.1:8787/lab/ats/journey"


def _evidence(final=GH, **changes):
    value = {
        "provider": "greenhouse",
        "ats": "greenhouse",
        "structured_feed": "greenhouse:acme",
        "employer": "Acme",
        "role": "Analyst",
        "requisition": "123456",
        "form_identity": "123456",
        "origin": origin_for_url(final),
    }
    value.update(changes)
    return value


def _target(final=GH, source=GH, ev=None, provider="greenhouse", form=False):
    return TargetResolution(
        source_url=source,
        final_url=final,
        kind=TargetKind.APPLICATION_FORM if form else TargetKind.APPLICATION_ENTRY,
        provider=provider,
        identity_verified=True,
        form_verified=form,
        evidence=ev if ev is not None else _evidence(final),
    )


def _form_evidence(final=GH, action="/submit", bound=None):
    return _evidence(
        final,
        form={
            "control_count": 1,
            "submit_present": True,
            "root_selector": "#application",
            "root_token": "123456",
            "form_identity": "123456",
            "binding_verified": True,
            "bound_target_url": bound or final,
            "bound_provider": "greenhouse",
            "action": action,
        },
    )


def _opportunity(session: Session, source=GH, employer="Acme", role="Analyst"):
    opp = Opportunity(
        employer=employer,
        role_title=role,
        programme_group="summer",
        cycle="2027",
        url=source,
        source="manual",
        ats_type="greenhouse",
        application_window_status="OPEN",
    )
    session.add(opp)
    session.flush()
    return opp


def _resolve_promoted(resolution, source=GH, employer="Acme", role="Analyst"):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            opp = _opportunity(session, source, employer, role)
            svc = TargetResolutionService(session)
            svc.resolve(opp.id, resolver=lambda context: resolution)
            session.flush()
            session.refresh(opp)
            reasons = json.loads(opp.resolution_evidence_json).get("reason_codes", [])
            return bool(opp.automation_url), opp.target_status, reasons
    finally:
        engine.dispose()


def _record_promoted(resolution, source=GH, employer="Acme", role="Analyst"):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            opp = _opportunity(session, source, employer, role)
            svc = TargetResolutionService(session)
            svc.record(opp.id, resolution)
            session.flush()
            session.refresh(opp)
            reasons = json.loads(opp.resolution_evidence_json).get("reason_codes", [])
            loader_ok = True
            try:
                _load_verified_target_from_opportunity(opp)
            except Exception:
                loader_ok = False
            return bool(opp.automation_url), opp.target_status, reasons, loader_ok
    finally:
        engine.dispose()


# 1. boolean strings must not verify custom domain (property level).
@pytest.mark.parametrize("marker", ["false", "0", "no"])
def test_custom_flag_false_like_does_not_verify(marker):
    ev = _evidence(CUSTOM, custom_domain_verified=marker)
    ev.pop("structured_feed")
    r = _target(final=CUSTOM, source=CUSTOM, ev=ev)
    assert r.verified_for_automation is False


def test_custom_flag_true_marker_alone_is_not_automation_proof():
    # A caller-/page-authored ``custom_domain_verified=True`` boolean is not
    # independent proof; no demonstrated producer emits a stronger
    # custom-domain identity bundle.  The marker stays detector metadata and
    # the custom target stays unverified for automation.
    ev = _evidence(CUSTOM, custom_domain_verified=True)
    ev.pop("structured_feed")
    r = _target(final=CUSTOM, source=CUSTOM, ev=ev)
    assert r.verified_for_automation is False


def test_trusted_provider_positive_control_verifies():
    r = _target(ev=_evidence())
    assert r.verified_for_automation is True


@pytest.mark.parametrize("marker", [False, 0, None, ""])
def test_custom_flag_falsy_does_not_verify(marker):
    ev = _evidence(CUSTOM, custom_domain_verified=marker)
    ev.pop("structured_feed")
    r = _target(final=CUSTOM, source=CUSTOM, ev=ev)
    assert r.verified_for_automation is False


# 1b. custom final URL is not independent provider proof: no promotion even
# with boolean True when the stored source is a trusted requisition.
def test_custom_true_from_trusted_store_does_not_promote():
    r = _target(final=CUSTOM, ev=_evidence(CUSTOM, custom_domain_verified=True))
    promoted, status, _ = _resolve_promoted(r)
    assert promoted is False
    assert status == "UNRESOLVED"


def test_custom_http_and_nondefault_port_do_not_verify():
    for url in (
        "http://careers.attacker.test/jobs/123456",
        "https://careers.attacker.test:8443/jobs/123456",
    ):
        ev = _evidence(CUSTOM, custom_domain_verified=True)
        ev.pop("structured_feed")
        ev["origin"] = url.rsplit("/jobs/", 1)[0]
        r = _target(final=url, source=url, ev=ev)
        assert r.verified_for_automation is False


# 2. shared contract: form actions resolved against final_url.
def test_contract_accepts_relative_same_origin_action():
    r = _target(ev=_form_evidence(action="/submit"), form=True)
    validate_target_resolution_contract(r)  # must not raise


def test_contract_accepts_restored_relative_action():
    r = _target(ev=_form_evidence(action="/submit"), form=True)
    validate_target_resolution_contract(r)


def test_contract_accepts_empty_action():
    ev = _form_evidence(action="")
    r = _target(ev=ev, form=True)
    validate_target_resolution_contract(r)


@pytest.mark.parametrize(
    "action",
    [
        "//attacker.test/submit",
        "https://attacker.test/submit",
        "javascript:exfiltrate()",
        "data:text/html,test",
    ],
)
def test_contract_rejects_hostile_actions(action):
    r = _target(ev=_form_evidence(action=action), form=True)
    with pytest.raises(ValueError):
        validate_target_resolution_contract(r)


@pytest.mark.parametrize(
    "action",
    [
        "wss://boards.greenhouse.io/submit",
        "ws://boards.greenhouse.io/submit",
    ],
)
def test_contract_rejects_websocket_actions(action):
    # Same-origin websocket actions still escape the HTTP(S)-only form
    # contract.  origin_for_url() maps wss->https, so the shared contract
    # must check the resolved scheme explicitly, not the mapped origin.
    r = _target(ev=_form_evidence(action=action), form=True)
    with pytest.raises(ValueError):
        validate_target_resolution_contract(r)


def test_contract_rejects_mapping_valued_action():
    r = _target(ev=_form_evidence(action={"target": "/submit"}), form=True)
    with pytest.raises(ValueError):
        validate_target_resolution_contract(r)


def test_contract_rejects_numeric_action():
    r = _target(ev=_form_evidence(action=123), form=True)
    with pytest.raises(ValueError):
        validate_target_resolution_contract(r)


def test_hostile_action_does_not_promote_and_restored_does():
    hostile = _target(ev=_form_evidence(action="//attacker.test/submit"), form=True)
    promoted, status, _ = _resolve_promoted(hostile)
    assert promoted is False
    restored = _target(ev=_form_evidence(action="/submit"), form=True)
    promoted, status, _ = _resolve_promoted(restored)
    assert promoted is True


@pytest.mark.parametrize(
    "action",
    [
        "wss://boards.greenhouse.io/submit",
        {"target": "/submit"},
        123,
    ],
)
def test_record_rejects_websocket_mapping_and_numeric_actions(action):
    r = _target(ev=_form_evidence(action=action), form=True)
    promoted, status, reasons, loader_ok = _record_promoted(r)
    assert promoted is False
    assert status == "UNRESOLVED"
    assert loader_ok is False
    assert any("contract" in str(code) for code in reasons)


def test_wss_action_does_not_promote_and_restored_does():
    hostile = _target(
        ev=_form_evidence(action="wss://boards.greenhouse.io/submit"), form=True
    )
    promoted, status, reasons = _resolve_promoted(hostile)
    assert promoted is False
    assert status == "UNRESOLVED"
    assert any("contract" in str(code) for code in reasons)
    restored = _target(ev=_form_evidence(action="/submit"), form=True)
    promoted, _, _ = _resolve_promoted(restored)
    assert promoted is True


# 3. record() cannot bypass resolve validation.
def test_record_foreign_bound_target_does_not_promote():
    foreign = _target(
        ev=_form_evidence(bound="https://boards.greenhouse.io/other/jobs/123456"),
        form=True,
    )
    promoted, status, reasons, loader_ok = _record_promoted(foreign)
    assert promoted is False
    assert status == "UNRESOLVED"
    assert loader_ok is False
    assert any("contract" in str(code) or "contradict" in str(code) for code in reasons)


def test_record_positive_and_restored_promote():
    for bound in (GH, GH):
        r = _target(ev=_form_evidence(bound=bound), form=True)
        promoted, status, _, loader_ok = _record_promoted(r)
        assert promoted is True
        assert loader_ok is True


# 4. employer/role bound to authoritative stored opportunity.
def test_resolve_wrong_employer_does_not_promote():
    r = _target(
        ev=_evidence(employer="Wrong Employer", structured_feed="greenhouse:wrong-employer")
    )
    promoted, status, reasons = _resolve_promoted(r)
    assert promoted is False
    assert status == "UNRESOLVED"


def test_resolve_wrong_role_does_not_promote():
    r = _target(ev=_evidence(role="Engineer"))
    promoted, status, reasons = _resolve_promoted(r)
    assert promoted is False
    assert status == "UNRESOLVED"


def test_resolve_ordinary_trusted_and_restored_promote():
    r = _target(ev=_evidence())
    promoted, _, _ = _resolve_promoted(r)
    assert promoted is True
    restored = _target(ev=_evidence())
    promoted, _, _ = _resolve_promoted(restored)
    assert promoted is True


@pytest.mark.parametrize(
    ("missing", "reason_code"),
    [
        ("employer", "resolution_employer_missing"),
        ("role", "resolution_role_missing"),
        ("form_identity", "resolution_form_identity_missing"),
    ],
)
def test_resolve_missing_identity_does_not_promote(missing, reason_code):
    ev = _evidence()
    ev.pop(missing)
    r = _target(ev=ev)
    promoted, status, reasons = _resolve_promoted(r)
    assert promoted is False
    assert status == "UNRESOLVED"
    assert reason_code in reasons


@pytest.mark.parametrize(
    ("missing", "reason_code"),
    [
        ("employer", "resolution_employer_missing"),
        ("role", "resolution_role_missing"),
        ("form_identity", "resolution_form_identity_missing"),
    ],
)
def test_record_missing_identity_does_not_promote(missing, reason_code):
    ev = _evidence()
    ev.pop(missing)
    r = _target(ev=ev)
    promoted, status, reasons, loader_ok = _record_promoted(r)
    assert promoted is False
    assert status == "UNRESOLVED"
    assert reason_code in reasons
    assert loader_ok is False


def test_resolve_wrong_tenant_and_requisition_do_not_promote():
    wrong_tenant = _target(
        final=GH.replace("/acme/", "/wrongemployer/"), ev=_evidence()
    )
    promoted, _, _ = _resolve_promoted(wrong_tenant)
    assert promoted is False
    wrong_req = _target(ev=_evidence(requisition="999999"))
    promoted, _, _ = _resolve_promoted(wrong_req)
    assert promoted is False


# Loopback synthetic positive preserved.
def test_loopback_synthetic_positive_preserved():
    r = TargetResolution(
        source_url=LOOPBACK,
        final_url=LOOPBACK,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"synthetic_lab": True},
    )
    assert r.verified_for_automation is True
