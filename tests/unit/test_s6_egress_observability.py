"""Tests for Season 6 egress observability — Task 1.

Asserts that _egress_stop_result includes fatal and tolerable egress records
in the manifest when set_egress_fatal_records has been called, without changing
any allow/deny decision.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.automation.classifier import DeterministicClassifier
from app.automation.runner import _OwnerThreadJourney
from app.automation.targets import TargetResolution
from app.automation.types import InspectedField, RunMode
from app.domain.questions import FormQuestion
from app.domain.targets import TargetKind
from app.models import Opportunity


def _make_journey(
    app_id: str = "s6-test-001",
    mode: RunMode = RunMode.PREFILL,
) -> _OwnerThreadJourney:
    """Create a minimal _OwnerThreadJourney for testing."""

    class _Adapter:
        name = "greenhouse"

        @staticmethod
        def detect_human_boundary(_page: object) -> tuple[bool, str]:
            return False, ""

        @staticmethod
        def inspect_with_evidence(_page: object):
            return [], {}

        def fill(self, _scope: object, _inspected: InspectedField, _value: str) -> None:
            pass

        @staticmethod
        def verify_step(_scope: object, _fields: object, _values: object) -> bool:
            return True

    class _Registry:
        def __init__(self) -> None:
            self.adapter = _Adapter()

        def detect(self, _resolution: TargetResolution) -> _Adapter:
            return self.adapter

    url = "https://boards.greenhouse.io/testcorp/jobs/123"
    resolver = SimpleNamespace(
        registry=_Registry(),
        classifier=DeterministicClassifier(),
    )
    resolution = TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={},
    )
    opportunity = Opportunity(
        employer="TestCorp",
        role_title="Test Role",
        url=url,
        cycle="2027",
    )
    return _OwnerThreadJourney(
        resolver,
        application_id=app_id,
        opportunity=opportunity,
        resolution=resolution,
        mode=mode,
        profile_values={},
        answers={},
        documents={},
    )


def test_egress_stop_includes_records_in_manifest() -> None:
    """_egress_stop_result includes fatal and tolerable egress records in the
    manifest when set_egress_fatal_records has been called, without changing
    the allow/deny decision (proven by the NEEDS_USER + blocked_reasons).

    The same request set is blocked before and after: this test verifies that
    the _egress_stop_result returns a NEEDS_USER result with the same
    blocked_reasons as before the change, and that the egress records are
    attached to the manifest as observability-only data.
    """
    journey = _make_journey()

    # Set the probe so _egress_stop_result returns a result
    journey.set_egress_fatal_probe(lambda: "test_fatal_block")

    # Set egress records: one fatal, one tolerable
    fatal_record = {
        "host": "evil-tracker.example.com",
        "path": "/pixel.gif",
        "method": "GET",
        "resource_type": "image",
        "initiator": "https://boards.greenhouse.io/testcorp/jobs/123",
        "is_navigation_request": False,
        "classification": "other",
        "impact": "fatal",
        "impact_reason": "unknown_resource_blocked",
        "fatal": True,
        "record_only": False,
        "allowed": False,
        "origin": "https://boards.greenhouse.io",
    }
    tolerable_record = {
        "host": "job-boards.cdn.greenhouse.io",
        "path": "/locales/en/common.d4e5f6.json",
        "method": "GET",
        "resource_type": "fetch",
        "initiator": "https://boards.greenhouse.io/testcorp/jobs/123",
        "is_navigation_request": False,
        "classification": "other",
        "impact": "tolerable",
        "impact_reason": "first_party_service_on_same_registrable_domain",
        "fatal": False,
        "record_only": True,
        "allowed": False,
        "origin": "https://boards.greenhouse.io",
    }
    journey.set_egress_fatal_records([fatal_record, tolerable_record])

    result = journey._egress_stop_result(stage="test")

    # The result MUST be a NEEDS_USER with the same blocked_reasons as before
    # the change — prove the allow/deny decision is unchanged.
    assert result is not None, "Expected a non-None egress stop result"
    assert result["state"] == "NEEDS_USER"
    assert "egress_functional_or_unknown_blocked" in result["blocked_reasons"]

    # The manifest MUST include the egress records as observability data.
    manifest = result.get("manifest", {})
    assert isinstance(manifest, dict)
    assert manifest.get("egress_stage") == "test"

    # Fatal requests must be present
    fatal_requests = manifest.get("egress_fatal_requests", [])
    assert len(fatal_requests) == 1, f"Expected 1 fatal request, got {len(fatal_requests)}"
    assert fatal_requests[0]["host"] == "evil-tracker.example.com"
    assert fatal_requests[0]["impact"] == "fatal"
    assert fatal_requests[0]["resource_type"] == "image"

    # Tolerable requests must be present
    tolerable_requests = manifest.get("egress_tolerable_requests", [])
    assert len(tolerable_requests) == 1, f"Expected 1 tolerable request, got {len(tolerable_requests)}"
    assert tolerable_requests[0]["host"] == "job-boards.cdn.greenhouse.io"
    assert tolerable_requests[0]["impact"] == "tolerable"

    # The same request set is blocked before and after: the blocked_reasons
    # are identical to what _egress_stop_result would return without records.
    assert result["blocked_reasons"] == ("egress_functional_or_unknown_blocked",)
    assert result["risk_level"] == 3
    assert result["reason"] == "Filling stopped after a functional or unclassified request was blocked"


def test_egress_stop_no_records_when_not_set() -> None:
    """_egress_stop_result works normally when no records have been set.

    This proves backward compatibility: without set_egress_fatal_records,
    the manifest does not contain egress_fatal_requests or
    egress_tolerable_requests keys.
    """
    journey = _make_journey()
    journey.set_egress_fatal_probe(lambda: "test_fatal_block")
    # Do NOT call set_egress_fatal_records

    result = journey._egress_stop_result(stage="test")

    assert result is not None
    assert result["state"] == "NEEDS_USER"
    manifest = result.get("manifest", {})
    assert isinstance(manifest, dict)
    # The egress records keys must NOT be present when no records were set
    assert "egress_fatal_requests" not in manifest
    assert "egress_tolerable_requests" not in manifest