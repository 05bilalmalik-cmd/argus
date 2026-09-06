from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.automation.classifier import DeterministicClassifier
from app.automation import runner as runner_module
from app.automation.runner import AutomationRunner, _OwnerThreadJourney
from app.automation.targets import TargetResolution
from app.automation.types import InspectedField, RunMode, SessionState
from app.config import Settings
from app.domain.questions import FormQuestion
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.services.navigator import ApplicationNavigator, HeadedSessionWorker


@pytest.mark.parametrize(
    ("resource_type", "method", "url"),
    [
        ("font", "GET", "https://cdn.example.test/inter.woff2"),
        ("image", "GET", "https://analytics.example.test/pixel"),
        ("stylesheet", "GET", "https://cdn.example.test/theme.css"),
        ("media", "GET", "https://cdn.example.test/intro.mp4"),
        ("texttrack", "GET", "https://cdn.example.test/captions.vtt"),
        ("ping", "POST", "https://telemetry.example.test/beacon"),
    ],
)
def test_unmeasured_resource_types_are_unknown_and_fatal(
    resource_type: str,
    method: str,
    url: str,
) -> None:
    """Adding a resource-type heuristic would turn an unknown request optional."""

    from app.automation.host_policy import (
        BlockedRequestImpact,
        classify_blocked_request_impact,
    )

    decision = classify_blocked_request_impact(
        url=url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=False,
        mode=RunMode.PREFILL.value,
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "unknown_resource_blocked"


@pytest.mark.parametrize(
    ("resource_type", "method", "is_navigation_request"),
    [
        ("xhr", "GET", False),
        ("fetch", "GET", False),
        ("xhr", "POST", False),
        ("fetch", "POST", False),
        ("document", "POST", True),
        ("script", "GET", False),
        ("", "GET", None),
        ("other", "GET", False),
    ],
)
def test_functional_navigation_and_unknown_blocks_are_fatal(
    resource_type: str,
    method: str,
    is_navigation_request: bool | None,
) -> None:
    """Adding any ambiguous type to the tolerable branch would expose this break."""

    from app.automation.host_policy import (
        BlockedRequestImpact,
        classify_blocked_request_impact,
    )

    decision = classify_blocked_request_impact(
        url="https://ats.example.test/application/validate",
        method=method,
        resource_type=resource_type,
        is_navigation_request=is_navigation_request,
        mode=RunMode.PREFILL.value,
    )

    assert decision.impact is BlockedRequestImpact.FATAL


def test_classification_is_prefill_only_and_never_relaxes_submit() -> None:
    """Applying the new consequence policy to SUBMIT would make this fail."""

    from app.automation.host_policy import (
        BlockedRequestImpact,
        classify_blocked_request_impact,
    )

    decision = classify_blocked_request_impact(
        url="https://cdn.example.test/inter.woff2",
        method="GET",
        resource_type="font",
        is_navigation_request=False,
        mode=RunMode.SUBMIT.value,
    )

    assert decision.impact is BlockedRequestImpact.FATAL
    assert decision.reason == "impact_classification_not_prefill"


@dataclass
class _Frame:
    url: str


class _Request:
    def __init__(
        self,
        *,
        url: str,
        method: str = "GET",
        resource_type: str = "font",
        headers: dict[str, str] | None = None,
        post_data: str = "",
        initiator: str = "http://127.0.0.1:8787/lab/ats/phase17",
        navigation: bool = False,
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {}
        self.post_data = post_data
        self.frame = _Frame(initiator)
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self) -> None:
        self.continued += 1

    def abort(self, reason: str) -> None:
        self.aborted.append(reason)


def _worker(*, enabled: bool, mode: RunMode = RunMode.PREFILL) -> HeadedSessionWorker:
    worker = HeadedSessionWorker(
        session_id=f"phase17-{mode.value}-{enabled}",
        application_id="application-phase17",
        mode=mode.value,
        url="http://127.0.0.1:8787/lab/ats/phase17",
        summary={"provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=True,
        allowlist=frozenset({"127.0.0.1"}),
        egress_impact_classification_enabled=enabled,
    )
    worker.owner_thread_id = threading.get_ident()
    return worker


@pytest.mark.parametrize(
    ("candidate_request", "expected_continued", "expected_aborted"),
    [
        (
            _Request(url="http://localhost:8787/assets/inter.woff2", resource_type="font"),
            0,
            ["blockedbyclient"],
        ),
        (
            _Request(
                url="http://127.0.0.1:8787/api/validate?email=alex%40example.test",
                resource_type="xhr",
            ),
            0,
            ["blockedbyclient"],
        ),
        (
            _Request(
                url="http://127.0.0.1:8787/application/upload",
                method="POST",
                resource_type="fetch",
                post_data="candidate-file",
            ),
            1,
            [],
        ),
    ],
)
def test_effective_route_policy_is_identical_when_classification_is_enabled(
    candidate_request: _Request,
    expected_continued: int,
    expected_aborted: list[str],
) -> None:
    """A newly reachable host/request would change the continue/abort tuple."""

    disabled = _worker(enabled=False)
    enabled = _worker(enabled=True)
    legacy_route = _Route(candidate_request)
    classified_route = _Route(candidate_request)

    disabled._route(legacy_route)
    enabled._route(classified_route)

    assert (classified_route.continued, classified_route.aborted) == (
        legacy_route.continued,
        legacy_route.aborted,
    )
    assert classified_route.continued == expected_continued
    assert classified_route.aborted == expected_aborted
    assert set(disabled._egress_records[-1]) == {
        "url",
        "method",
        "classification",
        "origin",
        "allowed",
        "fatal",
        "record_only",
        "reason",
    }


def test_tolerable_blocks_are_named_in_handoff_reason_and_manifest() -> None:
    """Dropping classified evidence before handoff would make these assertions fail."""

    worker = _worker(enabled=True)
    worker._route(
        _Route(
            _Request(
                url="https://www.dropbox.com/static/api/2/dropins.js",
                resource_type="script",
            )
        )
    )
    worker._route(
        _Route(
            _Request(
                url="https://apis.google.com/js/api.js",
                resource_type="script",
            )
        )
    )

    worker._emit_journey_result(
        {
            "state": "NEEDS_USER",
            "reason": "Approved fields were prefilled; human review is required",
            "risk_level": 3,
            "blocked_reasons": ("required_answer",),
            "manifest": {
                "prefilled_fields": ["first_name", "last_name", "email"],
                "failed_fields": [],
                "submission": "not_clicked",
            },
        }
    )

    result = worker._journey_result
    assert result["state"] == "NEEDS_USER"
    assert "filled; 2 non-essential resources blocked" in result["reason"]
    manifest = result["manifest"]
    assert manifest["nonessential_resources_blocked"] == 2
    assert manifest["egress_notice"] == "filled; 2 non-essential resources blocked"
    assert [item["resource_type"] for item in manifest["blocked_resources"]] == [
        "script",
        "script",
    ]
    assert manifest["blocked_resources"][0]["host"] == "www.dropbox.com"
    assert manifest["blocked_resources"][0]["path"] == "/static/api/2/dropins.js"
    assert manifest["blocked_resources"][0]["initiator"] == (
        "http://127.0.0.1:8787/lab/ats/phase17"
    )
    assert all(item["impact"] == "tolerable" for item in manifest["blocked_resources"])


@pytest.mark.parametrize(
    "blocked_request",
    [
        _Request(
            url="http://127.0.0.1:8787/api/validate?email=alex%40example.test",
            resource_type="xhr",
        ),
        _Request(
            url="http://localhost:8787/assets/opaque",
            resource_type="",
        ),
    ],
)
def test_fatal_or_unclassifiable_block_forces_human_handoff(
    blocked_request: _Request,
) -> None:
    """Treating XHR or missing metadata as cosmetic would make this fail."""

    worker = _worker(enabled=True)
    worker._route(_Route(blocked_request))

    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {"submission": "not_clicked"},
        }
    )

    result = worker._journey_result
    assert result["state"] == "NEEDS_USER"
    assert result["risk_level"] >= 3
    assert "functional or unclassified request" in result["reason"]
    assert "egress_functional_or_unknown_blocked" in result["blocked_reasons"]
    assert result["manifest"]["blocked_resources"][0]["impact"] == "fatal"
    assert worker._state is SessionState.HUMAN_REQUIRED


def test_candidate_bearing_display_resource_remains_fatal() -> None:
    """A cosmetic resource type must not override the host policy's data verdict."""

    worker = _worker(enabled=True)
    route = _Route(
        _Request(
            url="http://127.0.0.1:8787/pixel?email=demo%40example.test",
            resource_type="image",
        )
    )

    worker._route(route)
    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {"submission": "not_clicked"},
        }
    )

    blocked = worker._journey_result["manifest"]["blocked_resources"]
    assert route.aborted == ["blockedbyclient"]
    assert worker._journey_result["state"] == "NEEDS_USER"
    assert blocked[0]["classification"] == "data_bearing"
    assert blocked[0]["impact"] == "fatal"
    assert blocked[0]["impact_reason"] == "data_bearing_request_blocked"


@pytest.mark.parametrize(
    "blocked_request",
    [
        _Request(
            url="http://localhost:8787/application/upload?email=alex%40example.test",
            method="POST",
            resource_type="fetch",
            post_data="email=alex%40example.test&file=candidate.pdf",
        ),
        _Request(
            url="http://localhost:8787/application/submit?email=alex%40example.test",
            method="POST",
            resource_type="document",
            post_data="first_name=Alex&email=alex%40example.test",
            navigation=True,
        ),
    ],
)
def test_upload_endpoint_and_form_action_blocks_force_human_handoff(
    blocked_request: _Request,
) -> None:
    worker = _worker(enabled=True)
    route = _Route(blocked_request)

    worker._route(route)
    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {"submission": "not_clicked"},
        }
    )

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._journey_result["state"] == "NEEDS_USER"
    assert worker._journey_result["manifest"]["blocked_resources"][0]["impact"] == "fatal"


def test_missing_request_url_is_recorded_and_forces_human_handoff() -> None:
    worker = _worker(enabled=True)
    route = _Route(_Request(url="", resource_type=""))

    worker._route(route)
    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {"submission": "not_clicked"},
        }
    )

    assert route.aborted == ["blockedbyclient"]
    assert worker._journey_result["state"] == "NEEDS_USER"
    blocked = worker._journey_result["blocked_requests"]
    assert len(blocked) == 1
    assert blocked[0]["impact"] == "fatal"
    assert blocked[0]["impact_reason"] == "invalid_request_url"


def test_tolerable_blocks_survive_authoritative_final_manifest() -> None:
    worker = _worker(enabled=True)
    worker._route(
        _Route(
            _Request(
                url="https://www.dropbox.com/static/api/2/dropins.js",
                resource_type="script",
            )
        )
    )
    final_url = "http://127.0.0.1:8787/lab/ats/phase17"
    worker._emit_journey_result(
        {
            "state": "FINAL_REVIEW",
            "reason": "All approved fields were filled",
            "risk_level": 0,
            "blocked_reasons": (),
            "manifest": {
                "application_id": "application-phase17",
                "employer": "Example Employer",
                "role": "Analyst",
                "provider": "greenhouse",
                "requisition": "/lab/ats/phase17",
                "final_url": final_url,
                "target_fingerprint": "target-phase17",
                "control_fingerprint": "control-phase17",
                "form_action": f"{final_url}/submit",
                "method": "POST",
                "expected_receipt_url": f"{final_url}/receipt",
                "submission": "not_clicked",
            },
        }
    )

    result_manifest = worker._journey_result["manifest"]
    assert result_manifest["nonessential_resources_blocked"] == 1
    assert result_manifest["egress_notice"] == "filled; 1 non-essential resource blocked"
    assert result_manifest["blocked_resources"][0]["path"] == "/static/api/2/dropins.js"
    assert worker._manifest == result_manifest


def test_application_navigator_propagates_explicit_disabled_mode_to_normal_worker() -> None:
    captured: dict[str, object] = {}

    class _Worker:
        owner_thread_id = None
        cleanup_complete = False

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    url = "http://127.0.0.1:8787/lab/ats/phase17"
    resolution = TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "application_origin": "http://127.0.0.1:8787",
            "employer": "Example Employer",
            "role": "Analyst",
            "requisition": "/lab/ats/phase17",
            "form_identity": "phase17-form",
        },
    )
    navigator = ApplicationNavigator(
        settings=SimpleNamespace(egress_impact_classification_enabled=False),
        worker_factory=_Worker,
        headless=True,
    )

    navigator.start(
        "application-phase17-disabled",
        RunMode.PREFILL,
        resolution=resolution,
        summary={"provider": "greenhouse"},
    )

    assert captured["egress_impact_classification_enabled"] is False


def test_fatal_request_stops_prefill_before_remaining_fields_are_touched() -> None:
    class _Adapter:
        name = "greenhouse"

        def __init__(self) -> None:
            self.filled: list[str] = []

        @staticmethod
        def detect_human_boundary(_page: object) -> tuple[bool, str]:
            return False, ""

        @staticmethod
        def inspect_with_evidence(_page: object):
            def field(name: str, label: str) -> InspectedField:
                return InspectedField(
                    selector=f"#{name}",
                    question=FormQuestion(
                        label=label,
                        name=name,
                        field_type="text",
                        required=True,
                    ),
                    control_type="text",
                )

            return [
                field("first_name", "First name"),
                field("last_name", "Last name"),
                field("employer_question", "Employer-specific required question"),
            ], {
                "root_found": True,
                "page_root_found": True,
                "root_selector": "#application-form",
                "root_token": "phase17-root",
                "submit_present": True,
            }

        def fill(self, _scope: object, inspected: InspectedField, _value: str) -> None:
            self.filled.append(inspected.question.name)

        @staticmethod
        def verify_step(_scope: object, _fields: object, _values: object) -> bool:
            return True

    class _Registry:
        def __init__(self, adapter: _Adapter) -> None:
            self.adapter = adapter

        def detect(self, _resolution: TargetResolution) -> _Adapter:
            return self.adapter

    adapter = _Adapter()
    url = "http://127.0.0.1:8787/lab/ats/phase17"
    journey = _OwnerThreadJourney(
        SimpleNamespace(
            registry=_Registry(adapter),
            classifier=DeterministicClassifier(),
        ),
        application_id="application-phase17-stop",
        opportunity=Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group="summer",
            cycle="2027",
            url=url,
        ),
        resolution=TargetResolution(
            source_url=url,
            final_url=url,
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            evidence={"synthetic_lab": True},
        ),
        mode=RunMode.PREFILL,
        profile_values={
            "identity.first_name": "Alex",
            "identity.last_name": "Sample",
        },
        answers={},
        documents={},
    )
    journey.set_egress_fatal_probe(
        lambda: "same_origin_xhr_blocked" if adapter.filled else ""
    )
    journey.set_egress_fatal_records([])
    page = SimpleNamespace(url=url, evaluate=lambda _script: {})

    result = journey._run_steps(page)

    assert adapter.filled == ["first_name"]
    assert result["state"] == "NEEDS_USER"
    assert "egress_functional_or_unknown_blocked" in result["blocked_reasons"]
    assert result["manifest"]["prefilled_fields"] == ["first_name"]


@pytest.mark.parametrize(
    (
        "journey_state",
        "snapshot_state",
        "expected_outcome_state",
        "risk_level",
        "blocked_reasons",
    ),
    [
        (
            "NEEDS_USER",
            SessionState.HUMAN_REQUIRED,
            "NEEDS_USER",
            3,
            ("required_answer",),
        ),
        (
            "FINAL_REVIEW",
            SessionState.FINAL_REVIEW,
            "READY_TO_SUBMIT",
            0,
            (),
        ),
    ],
)
def test_runner_persists_query_free_blocked_evidence_and_needs_you_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    journey_state: str,
    snapshot_state: SessionState,
    expected_outcome_state: str,
    risk_level: int,
    blocked_reasons: tuple[str, ...],
) -> None:
    url = "http://127.0.0.1:8787/lab/ats/phase17"
    opportunity = Opportunity(
        id="opportunity-phase17-audit",
        employer="Example Employer",
        role_title="Analyst",
        programme_group="summer",
        cycle="2027",
        url=url,
        application_url=url,
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
    )
    application = Application(
        id="application-phase17-audit",
        opportunity_id=opportunity.id,
        state="FILLING",
        next_action="",
    )

    class _Session:
        def get(self, model: object, identifier: str):
            if model is Application and identifier == application.id:
                return application
            if model is Opportunity and identifier == opportunity.id:
                return opportunity
            return None

        def add(self, _value: object) -> None:
            return None

        def flush(self) -> None:
            return None

        def refresh(self, _value: object) -> None:
            return None

    class _Run:
        id = "run-phase17-audit"
        trace_path = ""
        screenshot_path = ""
        state = ""
        error = ""
        adapter = ""
        finished_at = None

        def __init__(self, **_kwargs: object) -> None:
            return None

        def mark_risk_assessed(self, *_args: object, **_kwargs: object) -> None:
            return None

    class _Adapter:
        name = "greenhouse"

    class _Registry:
        @staticmethod
        def detect(_resolution: TargetResolution) -> _Adapter:
            return _Adapter()

    egress_notice = "filled; 1 non-essential resource blocked"
    base_reason = (
        "Approved fields were prefilled; human review is required"
        if journey_state == "NEEDS_USER"
        else "All approved fields were filled; " + ("review evidence " * 24)
    )
    result = {
        "state": journey_state,
        "reason": f"{base_reason}; {egress_notice}",
        "risk_level": risk_level,
        "blocked_reasons": blocked_reasons,
        "adapter": "greenhouse",
        "blocked_requests": [
            {
                "url": "http://localhost:8787/assets/inter.woff2?candidate=secret",
                "host": "localhost",
                "path": "/assets/inter.woff2",
                "method": "GET",
                "resource_type": "font",
                "initiator": f"{url}?candidate=secret",
                "is_navigation_request": False,
                "classification": "passive_asset",
                "origin": "http://localhost:8787",
                "fatal": False,
                "record_only": True,
                "reason": "passive_asset_cross_origin_record_only",
                "impact": "tolerable",
                "impact_reason": "nonessential_font",
                "unexpected": "must-not-persist",
            }
        ],
        "manifest": {
            "egress_notice": egress_notice,
            "submission": "not_clicked",
        },
    }

    class _Navigator:
        @staticmethod
        def start(*_args: object, **_kwargs: object):
            return SimpleNamespace(session_id="session-phase17-audit")

        @staticmethod
        def run_journey(_session_id: str) -> None:
            return None

        @staticmethod
        def wait_for_journey_result(_session_id: str, *, timeout: int):
            return result

    resolution = TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"synthetic_lab": True},
    )
    audit_inputs: list[object] = []
    runner = object.__new__(AutomationRunner)
    runner.settings = SimpleNamespace(
        traces_dir=tmp_path / "traces",
        screenshots_dir=tmp_path / "screenshots",
    )
    runner.registry = _Registry()
    runner._handoff_manager = _Navigator()
    runner._active_navigator_lease = None
    monkeypatch.setattr(runner, "_resolution_from_opportunity", lambda _value: resolution)
    monkeypatch.setattr(
        runner,
        "_revalidate_review_target",
        lambda *_args, **_kwargs: (application, resolution),
    )
    monkeypatch.setattr(
        runner,
        "_approved_inputs",
        lambda *_args: ({}, {}, {}, {}, []),
    )
    monkeypatch.setattr(runner, "_navigator_for_run", lambda _headed: (_Navigator(), False))
    monkeypatch.setattr(
        runner,
        "_settled_handoff_snapshot",
        lambda *_args: SimpleNamespace(
            state=snapshot_state,
            human_boundary=(
                {"kind": "risk_review"}
                if snapshot_state is SessionState.HUMAN_REQUIRED
                else {}
            ),
        ),
    )
    monkeypatch.setattr(runner_module, "AutomationRun", _Run)
    monkeypatch.setattr(
        runner_module,
        "append_audit",
        lambda _session, audit_input: audit_inputs.append(audit_input),
    )

    outcome = runner._run_claimed_owner(
        _Session(),
        application,
        application.id,
        RunMode.PREFILL,
        headed=True,
    )

    assert outcome.state == expected_outcome_state
    assert egress_notice in application.next_action
    assert len(application.next_action) <= 240
    finished = next(
        item for item in audit_inputs if item.event_type == "automation.run_finished"
    )
    blocked = finished.details["blocked_requests"]
    assert blocked == [
        {
            "host": "localhost",
            "path": "/assets/inter.woff2",
            "method": "GET",
            "resource_type": "font",
            "initiator": "http://127.0.0.1:8787/lab/ats/phase17",
            "is_navigation_request": False,
            "classification": "passive_asset",
            "origin": "http://localhost:8787",
            "fatal": False,
            "record_only": True,
            "reason": "passive_asset_cross_origin_record_only",
            "impact": "tolerable",
            "impact_reason": "nonessential_font",
        }
    ]
    assert "candidate=secret" not in repr(finished.details)


def test_disabled_classification_preserves_legacy_journey_result_byte_for_byte() -> None:
    """Any disabled-mode result enrichment would violate rollback compatibility."""

    worker = _worker(enabled=False)
    worker._route(
        _Route(
            _Request(
                url="http://localhost:8787/assets/inter.woff2",
                resource_type="font",
            )
        )
    )
    original = {
        "state": "ACTIVE",
        "reason": "Read-only inspection complete",
        "risk_level": 0,
        "blocked_reasons": (),
        "manifest": {
            "prefilled_fields": ["email"],
            "submission": "not_clicked",
        },
    }

    worker._emit_journey_result(original)

    assert worker._journey_result == original


def test_submit_result_is_never_rewritten_by_phase17_classification() -> None:
    """A Phase 17 branch that alters SUBMIT results would make this fail."""

    worker = _worker(enabled=True, mode=RunMode.SUBMIT)
    worker._route(
        _Route(
            _Request(
                url="http://localhost:8787/assets/inter.woff2",
                resource_type="font",
            )
        )
    )
    original = {
        "state": "ACTIVE",
        "reason": "Existing submit-mode result",
        "risk_level": 4,
        "blocked_reasons": ("existing_guard",),
        "manifest": {"submission": "not_clicked"},
    }

    worker._emit_journey_result(original)

    assert worker._journey_result == original


def test_settings_enable_classification_by_default_with_explicit_legacy_off(tmp_path) -> None:
    """Losing either the safe default or rollback switch would make this fail."""

    enabled = Settings.load({"ARGUS_DATA_DIR": str(tmp_path / "enabled")})
    disabled = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path / "disabled"),
            "ARGUS_ENABLE_EGRESS_IMPACT_CLASSIFICATION": "false",
        }
    )

    assert enabled.egress_impact_classification_enabled is True
    assert disabled.egress_impact_classification_enabled is False
