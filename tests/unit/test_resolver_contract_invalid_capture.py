"""Capture the ``resolver_contract_invalid`` ValueError text and prove the fix.

Swallowing site: ``TargetResolutionService.resolve`` in
``app/services/target_resolution.py`` converted any ``ValueError`` from the
resolver callable (or from ``_resolver_result`` coercion) into the generic
``resolver_contract_invalid`` reason code and discarded the message.  The
underlying defect -- the headed endpoint adapter reaching the Navigator's
local egress gate with no one-call source grant -- was therefore invisible.

These tests pin:
1. the observability contract (reason code unchanged, sanitised detail kept
   in evidence and in the ``opportunity.target_resolved`` audit event);
2. the endpoint-adapter grant (public inspection URL gets an owned,
   URL-bound, revoked-after-call capability even when apply-click is off);
3. backward compatibility (legacy single-arg hooks and loopback fixtures
   keep the exact previous call shape);
4. the negative control (a genuinely different job's URL still fails the
   nested-proof contract -- the check is not weakened).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.automation.targets import (
    TargetResolution,
    validate_target_resolution_contract,
)
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, AuditEvent, Opportunity
from app.security.crypto import CryptoBox
from app.services.target_resolution import (
    ResolutionContext,
    TargetResolutionService,
)


VIRTU_URL = "https://job-boards.greenhouse.io/virtu/jobs/8547254002?gh_src=Trackr"
EGRESS_MESSAGE = f"navigation URL blocked by local egress policy: {VIRTU_URL}"


def _database(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _context(**overrides) -> ResolutionContext:
    values = {
        "opportunity_id": "opportunity-test",
        "application_id": "application-test",
        "source_url": "https://app.the-trackr.com/uk-finance/summer-internships",
        "employer": "Virtu Financial",
        "role_title": "2027 Internship - Quantitative Trading",
        "cycle": "2026-27",
        "provider_hint": "greenhouse",
        "source": "trackr_live:summer-internships",
        "inspection_url": VIRTU_URL,
    }
    values.update(overrides)
    return ResolutionContext(**values)


def test_contract_invalid_keeps_reason_and_records_detail(tmp_path: Path) -> None:
    """The pass/fail outcome is unchanged but the message now survives."""

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Virtu Financial",
            role_title="2027 Internship - Quantitative Trading",
            programme_group="summer",
            cycle="2026-27",
            url="https://app.the-trackr.com/uk-finance/summer-internships",
            source="trackr_live:summer-internships",
            ats_type="greenhouse",
            application_window_status="OPEN",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
        )
        session.add(application)
        session.flush()

        def egress_refusal(_context: ResolutionContext):
            raise ValueError(EGRESS_MESSAGE)

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=egress_refusal,
        )

        # Outcome unchanged: still fails closed with the generic code.
        assert list(outcome.reason_codes) == ["resolver_contract_invalid"]
        assert outcome.promoted is False

        # ... but the real message is now in the persisted evidence ...
        evidence = json.loads(opportunity.resolution_evidence_json or "{}")
        detail = evidence["evidence"]["resolver_error_detail"]
        assert "blocked by local egress policy" in detail
        assert "virtu/jobs/8547254002" in detail
        opportunity_id = opportunity.id

    # ... and in the audit event (queried in a fresh scope after commit).
    from sqlalchemy import select

    with database.session_scope() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_id == opportunity_id,
                    AuditEvent.event_type == "opportunity.target_resolved",
                )
            )
        )
        assert events, "expected an opportunity.target_resolved audit event"
        audit_details = json.loads(events[-1].details_json)
        assert audit_details["resolver_error_detail"] == detail


def test_contract_invalid_detail_redacts_url_tokens(tmp_path: Path) -> None:
    """Greenhouse ``token`` query values stay redacted in the diagnostic."""

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Demo Employer",
            role_title="Demo Role",
            programme_group="summer",
            cycle="2027",
            url="https://app.the-trackr.com/uk-finance/summer-internships",
            source="manual",
            ats_type="greenhouse",
            application_window_status="OPEN",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
        )
        session.add(application)
        session.flush()

        leaked = (
            "navigation URL blocked by local egress policy: "
            "https://job-boards.greenhouse.io/embed/job_app"
            "?for=gsacapital&token=8518528002"
        )

        def refusal(_context: ResolutionContext):
            raise ValueError(leaked)

        TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=refusal,
        )
        evidence = json.loads(opportunity.resolution_evidence_json or "{}")
        detail = evidence["evidence"]["resolver_error_detail"]
        assert "8518528002" not in detail
        assert "token=[redacted]" in detail


def test_endpoint_adapter_issues_owned_grant_without_apply_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public inspection URLs get a one-call grant on the endpoint path."""

    import app.services.navigator as navigator_module
    from app.services.target_resolution import NavigatorTargetResolver

    monkeypatch.setattr(
        navigator_module, "safe_public_navigation_url", lambda _url: True
    )
    seen: list[object] = []

    class Navigator:
        settings = SimpleNamespace(apply_click_enabled=False)

        @staticmethod
        def resolve_application_target(
            application_id: str,
            *,
            source_capability,
            headed: bool,
        ):
            assert application_id == "application-test"
            assert source_capability.active is True
            assert source_capability.hostname == "job-boards.greenhouse.io"
            seen.append(source_capability)
            return None, {}

    resolver = NavigatorTargetResolver(Navigator(), headed=True)
    assert resolver(_context()) == (None, {})
    assert len(seen) == 1
    # The owned grant is revoked after the single call.
    assert seen[0].active is False


def test_endpoint_adapter_keeps_legacy_single_arg_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy ``resolve_source(application_id)`` hooks keep the old shape."""

    import app.services.navigator as navigator_module
    from app.services.target_resolution import NavigatorTargetResolver

    monkeypatch.setattr(
        navigator_module, "safe_public_navigation_url", lambda _url: True
    )
    observed: list[object] = []

    class Navigator:
        settings = SimpleNamespace(apply_click_enabled=False)

        @staticmethod
        def resolve_source(exact_application_id: str):
            observed.append(exact_application_id)
            return None, {}

    resolver = NavigatorTargetResolver(Navigator())
    assert resolver(_context()) == (None, {})
    assert observed == ["application-test"]


def test_loopback_context_keeps_capability_less_call() -> None:
    """Lab/loopback fixtures cannot carry a grant: no behaviour change."""

    from app.services.target_resolution import NavigatorTargetResolver

    observed: dict[str, object] = {}

    class Navigator:
        settings = SimpleNamespace(apply_click_enabled=False)

        def resolve_application_target(self, application_id, *args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return None, {}

    resolver = NavigatorTargetResolver(Navigator())
    context = _context(
        source_url="http://127.0.0.1:8787/source/listing?id=demo",
        inspection_url="",
    )
    assert resolver(context) == (None, {})
    assert observed["args"] == ()
    assert observed["kwargs"] == {}


def _virtu_resolution(bound_target_url: str) -> TargetResolution:
    return TargetResolution(
        source_url=VIRTU_URL,
        final_url=VIRTU_URL,
        kind=TargetKind.APPLICATION_FORM,
        provider="greenhouse",
        identity_verified=True,
        form_verified=True,
        reason_codes=("verified_application_form",),
        evidence={
            "provider": "greenhouse",
            "employer": "Virtu Financial",
            "role": "2027 Internship - Quantitative Trading",
            "requisition": "8547254002",
            "bound_target_url": bound_target_url,
            "application_origin": "https://job-boards.greenhouse.io",
        },
    )


def test_matching_bound_target_passes_contract() -> None:
    validate_target_resolution_contract(_virtu_resolution(VIRTU_URL))


def test_different_job_url_still_fails_contract() -> None:
    """MANDATORY negative: a genuinely different job must not verify."""

    with pytest.raises(ValueError, match="contradict"):
        validate_target_resolution_contract(
            _virtu_resolution(
                "https://job-boards.greenhouse.io/virtu/jobs/9999999999"
                "?gh_src=Trackr"
            )
        )
