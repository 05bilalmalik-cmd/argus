from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.automation.runner import AutomationRunner, _query_free_first_party_requests
from app.automation.targets import TargetResolution
from app.automation.types import RunMode, SessionState
from app.config import Settings
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, AuditEvent, AutomationRun, Opportunity
from app.security.audit import AuditInput, append_audit


def _client(
    tmp_path: Path,
    *,
    ui_v2: bool = False,
    egress_enabled: bool = True,
) -> TestClient:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_UI_V2": "true" if ui_v2 else "false",
            "ARGUS_ENABLE_EGRESS_IMPACT_CLASSIFICATION": (
                "true" if egress_enabled else "false"
            ),
        }
    )
    return TestClient(create_app(settings))


def _seed_application(
    client: TestClient,
    *,
    employer: str,
    state: str = "FILLING",
) -> str:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title="First Party Evidence Analyst",
            programme_group="summer",
            location="London",
            cycle="2027",
            url="https://boards.greenhouse.io/acme/jobs/1234567",
            application_url="https://boards.greenhouse.io/acme/jobs/1234567",
            target_status=TargetKind.APPLICATION_ENTRY.value,
            resolved_ats_type="greenhouse",
            resolved_at=datetime.now(timezone.utc),
            application_window_status="OPEN",
            source="phase21-test",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=state,
            priority=70,
            eligibility_json='{"eligible": true, "reason_codes": []}',
            conflict_json='{"blocked": false, "reason_codes": []}',
        )
        session.add(application)
        session.flush()
        return application.id


class _Adapter:
    name = "greenhouse"


class _Registry:
    @staticmethod
    def detect(_resolution: TargetResolution) -> _Adapter:
        return _Adapter()


class _Navigator:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    @staticmethod
    def start(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(session_id="phase21-audit-session")

    @staticmethod
    def run_journey(_session_id: str) -> None:
        return None

    def wait_for_journey_result(self, _session_id: str, *, timeout: int) -> dict[str, object]:
        return self._result

    @staticmethod
    def get(_session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            state=SessionState.HUMAN_REQUIRED,
            human_boundary={"kind": "required_answer"},
        )


def _runner_finished_details(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    application_id: str,
    result: dict[str, object],
    mode: RunMode = RunMode.PREFILL,
    final_url: str = "https://boards.greenhouse.io/acme/jobs/1234567",
) -> dict[str, object]:
    resolution = TargetResolution(
        source_url="https://boards.greenhouse.io/acme/jobs/1234567",
        final_url=final_url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={"phase21_test": True},
    )
    navigator = _Navigator(result)
    runner = object.__new__(AutomationRunner)
    runner.settings = client.app.state.settings
    runner.registry = _Registry()
    runner._handoff_manager = navigator
    runner._active_navigator_lease = None
    monkeypatch.setattr(runner, "_resolution_from_opportunity", lambda _value: resolution)
    monkeypatch.setattr(
        runner,
        "_approved_inputs",
        lambda *_args: ({}, {}, {}, {}, []),
    )
    monkeypatch.setattr(runner, "_navigator_for_run", lambda _headed: (navigator, False))

    with client.app.state.db.session_scope() as session:
        application = session.get(Application, application_id)
        assert application is not None
        outcome = runner._run_claimed_owner(
            session,
            application,
            application_id,
            mode,
            headed=True,
        )
        run_id = outcome.run_id

    with client.app.state.db.session_scope() as session:
        finished = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "automation.run_finished",
                AuditEvent.entity_type == "automation_run",
                AuditEvent.entity_id == run_id,
            )
        )
        assert finished is not None
        details = json.loads(finished.details_json)
        assert isinstance(details, dict)
        return details


def _safe_email_request() -> dict[str, object]:
    return {
        "host": "email-address-validator.us.greenhouse.io",
        "path": "/address/validate",
        "method": "GET",
        "resource_type": "fetch",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": True,
        "candidate_data_kind": "email",
    }


def test_runner_persists_every_first_party_request_through_a_second_whitelist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel_email = "alex.runner.sentinel@example.test"
    sentinel_location = "221B Runner Sentinel Street"
    raw_email = {
        "host": "email-address-validator.us.greenhouse.io",
        "path": "/address/validate",
        "method": "GET",
        "resource_type": "fetch",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": True,
        "candidate_data_kind": "email",
        "url": f"https://email-address-validator.us.greenhouse.io/email-check?email={sentinel_email}",
        "query": f"email={sentinel_email}",
        "fragment": "candidate-fragment",
        "body": {"email": sentinel_email},
        "headers": {"x-candidate-email": sentinel_email},
        "email": sentinel_email,
        "location": sentinel_location,
        "unexpected": "must-not-persist",
    }
    raw_location = {
        "host": "api-geocode-earth-proxy.greenhouse.io",
        "path": "/v1/autocomplete",
        "method": "GET",
        "resource_type": "fetch",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": True,
        "candidate_data_kind": "location",
        "body": sentinel_location,
    }
    raw_no_candidate_data = {
        "host": "job-boards.cdn.greenhouse.io",
        "path": "/assets/flags-a2kmUSbF.webp",
        "method": "GET",
        "resource_type": "image",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": False,
        "candidate_data_kind": "",
        "headers": {"x-location": sentinel_location},
    }
    current_first_party = [raw_email] * 201 + [raw_location, raw_no_candidate_data]
    result = {
        "state": "NEEDS_USER",
        "reason": "Approved fields were prefilled; human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "first_party_requests": current_first_party,
        "manifest": {
            "submission": "not_clicked",
            "first_party_requests": current_first_party,
        },
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="Runner Boundary Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
        )

    expected_email = {
        "host": "email-address-validator.us.greenhouse.io",
        "path": "/address/validate",
        "method": "GET",
        "resource_type": "fetch",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": True,
        "candidate_data_kind": "email",
    }
    assert len(details.get("first_party_requests", [])) == 203
    assert details["first_party_requests"][:201] == [expected_email] * 201
    assert details["first_party_requests"][201:] == [
        {
            "host": "api-geocode-earth-proxy.greenhouse.io",
            "path": "/v1/autocomplete",
            "method": "GET",
            "resource_type": "fetch",
            "manifest_vendor": "greenhouse",
            "manifest_permission": True,
            "delivery": "blocked",
            "carries_candidate_data": True,
            "candidate_data_kind": "location",
        },
        {
            "host": "job-boards.cdn.greenhouse.io",
            "path": "/assets/flags-a2kmUSbF.webp",
            "method": "GET",
            "resource_type": "image",
            "manifest_vendor": "greenhouse",
            "manifest_permission": True,
            "delivery": "blocked",
            "carries_candidate_data": False,
            "candidate_data_kind": "",
        },
    ]
    persisted = repr(details)
    for forbidden in (
        sentinel_email,
        sentinel_location,
        "candidate-fragment",
        "unexpected",
    ):
        assert forbidden not in persisted


@pytest.mark.parametrize(
    ("change", "sentinel"),
    [
        ({"path": "/address/validate/alex.runner.path@example.test"}, "alex.runner.path@example.test"),
        ({"path": "/address/validate/alex%2Erunner%40example%2Etest"}, "alex%2Erunner%40example%2Etest"),
        ({"host": "unlisted.greenhouse.io"}, "unlisted.greenhouse.io"),
        ({"resource_type": "image"}, "image"),
        ({"candidate_data_kind": "location"}, "location"),
        ({"manifest_vendor": "GreenHouse"}, "GreenHouse"),
        (
            {
                "host": "job-boards.cdn.greenhouse.io",
                "path": "/assets/flags-AlexSample1999.webp",
                "resource_type": "image",
                "carries_candidate_data": False,
                "candidate_data_kind": "",
            },
            "AlexSample1999",
        ),
    ],
    ids=[
        "literal-pii-path",
        "percent-encoded-pii-path",
        "unlisted-host-with-true",
        "wrong-resource",
        "wrong-kind",
        "malformed-vendor",
        "pii-like-cdn-filename",
    ],
)
def test_runner_rejects_records_outside_the_measured_endpoint_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: dict[str, object],
    sentinel: str,
) -> None:
    request = {**_safe_email_request(), **change}
    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "first_party_requests": [request],
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="Runner Revalidation Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
        )

    assert details.get("first_party_requests") == []
    assert sentinel not in json.dumps(details)


@pytest.mark.parametrize(
    ("unsafe_path", "sentinel"),
    [
        (
            "/address/validate/alex.audit.path@example.test",
            "alex.audit.path@example.test",
        ),
        (
            "/address/validate/alex%2Eaudit%2Epath%40example%2Etest",
            "alex%2Eaudit%2Epath%40example%2Etest",
        ),
    ],
    ids=["literal-pii-path", "percent-encoded-pii-path"],
)
def test_runner_redacts_rejected_service_paths_from_blocked_request_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_path: str,
    sentinel: str,
) -> None:
    """The generic blocked projector must not retain a path rejected by the strict projector."""

    safe = _safe_email_request()
    safe_blocked = {
        **safe,
        "impact": "first_party_service",
        "impact_reason": "first_party_service_manifest_match",
    }
    unsafe = {**safe_blocked, "path": unsafe_path}
    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "blocked_requests": [safe_blocked, unsafe],
        "first_party_requests": [safe, unsafe],
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="Blocked Path Audit Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
        )

    assert [item["path"] for item in details["blocked_requests"]] == [
        "/address/validate",
        "",
    ]
    assert details["first_party_requests"] == [safe]
    assert sentinel not in json.dumps(details)


@pytest.mark.parametrize(
    ("unsafe_path", "sentinel"),
    [
        (
            "/address/validate/alex.only.blocked@example.test",
            "alex.only.blocked@example.test",
        ),
        (
            "/address/validate/alex%2Eonly%2Eblocked%40example%2Etest",
            "alex%2Eonly%2Eblocked%40example%2Etest",
        ),
    ],
    ids=["literal-pii-path", "percent-encoded-pii-path"],
)
def test_runner_redacts_an_unsafe_only_service_path_without_first_party_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_path: str,
    sentinel: str,
) -> None:
    """A missing first-party list must not disable PREFILL blocked-path privacy."""

    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("egress_functional_or_unknown_blocked",),
        "adapter": "greenhouse",
        "blocked_requests": [
            {
                **_safe_email_request(),
                "path": unsafe_path,
                "impact": "fatal",
                "impact_reason": "functional_or_unclassified_resource_blocked",
            }
        ],
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="Unsafe Only Audit Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
        )

    assert details["blocked_requests"][0]["path"] == ""
    assert "first_party_requests" not in details
    assert sentinel not in json.dumps(details)


@pytest.mark.parametrize(
    ("mode", "egress_enabled"),
    [
        (RunMode.PREFILL, False),
        (RunMode.REVIEW, True),
    ],
    ids=["disabled-prefill", "review"],
)
def test_runner_non_phase21_scopes_preserve_legacy_blocked_path_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: RunMode,
    egress_enabled: bool,
) -> None:
    """Disabled PREFILL and non-PREFILL must not inherit the new evidence contract."""

    legacy_path = "/legacy/provider-controlled-path"
    result = {
        "state": "NEEDS_USER",
        "reason": "Existing legacy result",
        "risk_level": 3,
        "blocked_reasons": ("existing_guard",),
        "adapter": "greenhouse",
        "blocked_requests": [
            {
                **_safe_email_request(),
                "path": legacy_path,
                "impact": "fatal",
                "impact_reason": "legacy_impact",
            }
        ],
        "first_party_requests": [_safe_email_request()],
    }

    with _client(tmp_path, egress_enabled=egress_enabled) as client:
        application_id = _seed_application(client, employer="Legacy Scope Audit Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
            mode=mode,
        )

    assert details["blocked_requests"][0]["path"] == legacy_path
    assert "first_party_requests" not in details


def test_runner_persists_canonical_head_first_party_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    head_request = {
        "host": "my.greenhouse.io",
        "path": "/users" + "/self",
        "method": "HEAD",
        "resource_type": "fetch",
        "manifest_vendor": "greenhouse",
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": False,
        "candidate_data_kind": "",
    }
    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "first_party_requests": [head_request],
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="HEAD Evidence Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
        )

    assert details.get("first_party_requests") == [head_request]


def test_runner_rejects_first_party_evidence_from_a_cross_vendor_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "first_party_requests": [_safe_email_request()],
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="Cross Vendor Runner Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
            final_url="https://jobs.lever.co/acme/role/apply",
        )

    assert details.get("first_party_requests") == []


@pytest.mark.parametrize(
    "mode",
    [RunMode.INSPECT, RunMode.REVIEW, RunMode.DRY_RUN],
    ids=["inspect", "review", "dry-run"],
)
def test_runner_keeps_first_party_key_out_of_non_prefill_audits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: RunMode,
) -> None:
    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "first_party_requests": [_safe_email_request()],
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer=f"{mode.value} Runner Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
            mode=mode,
        )

    assert "first_party_requests" not in details


def test_runner_keeps_first_party_key_out_when_prefill_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = {
        "state": "NEEDS_USER",
        "reason": "Human review is required",
        "risk_level": 3,
        "blocked_reasons": ("required_answer",),
        "adapter": "greenhouse",
        "manifest": {"submission": "not_clicked"},
    }

    with _client(tmp_path) as client:
        application_id = _seed_application(client, employer="Disabled Feature Runner Firm")
        details = _runner_finished_details(
            client,
            monkeypatch,
            application_id=application_id,
            result=result,
        )

    assert "first_party_requests" not in details


def test_runner_submit_gate_is_non_executing_and_emits_no_evidence() -> None:
    evidence = _query_free_first_party_requests(
        [_safe_email_request()],
        mode=RunMode.SUBMIT,
        current_page_url="https://boards.greenhouse.io/acme/jobs/1234567",
    )

    assert evidence is None


@pytest.mark.parametrize("ui_v2", [False, True], ids=["v1", "v2"])
def test_application_detail_associates_run_audits_and_discloses_only_safe_records(
    tmp_path: Path,
    ui_v2: bool,
) -> None:
    sentinel_email = "alex.ui.sentinel@example.test"
    sentinel_location = "10 UI Sentinel Square"
    with _client(tmp_path / ("v2" if ui_v2 else "v1"), ui_v2=ui_v2) as client:
        application_id = _seed_application(client, employer="Disclosure Firm", state="NEEDS_USER")
        other_application_id = _seed_application(
            client,
            employer="Other Application Firm",
            state="NEEDS_USER",
        )
        with client.app.state.db.session_scope() as session:
            run = AutomationRun(
                application_id=application_id,
                mode="prefill",
                state="NEEDS_USER",
                adapter="greenhouse",
            )
            other_run = AutomationRun(
                application_id=other_application_id,
                mode="prefill",
                state="NEEDS_USER",
                adapter="greenhouse",
            )
            session.add_all([run, other_run])
            session.flush()
            append_audit(
                session,
                AuditInput(
                    "system",
                    "application.evaluated",
                    "application",
                    application_id,
                    {"eligible": True},
                ),
            )
            safe_email = {
                "host": "email-address-validator.us.greenhouse.io",
                "path": "/address/validate",
                "method": "GET",
                "resource_type": "fetch",
                "manifest_vendor": "greenhouse",
                "manifest_permission": True,
                "delivery": "blocked",
                "carries_candidate_data": True,
                "candidate_data_kind": "email",
                "url": f"https://email-address-validator.us.greenhouse.io/address/validate?address={sentinel_email}",
                "body": sentinel_email,
                "headers": {"x-location": sentinel_location},
                "email": sentinel_email,
                "location": sentinel_location,
            }
            append_audit(
                session,
                AuditInput(
                    "automation",
                    "automation.run_finished",
                    "automation_run",
                    run.id,
                    {
                        "application_id": application_id,
                        "mode": "prefill",
                        "application_url": "https://boards.greenhouse.io/acme/jobs/1234567",
                        "first_party_requests": [
                            safe_email,
                            dict(safe_email),
                            {
                                "host": "api-geocode-earth-proxy.greenhouse.io",
                                "path": "/v1/autocomplete",
                                "method": "GET",
                                "resource_type": "fetch",
                                "manifest_vendor": "greenhouse",
                                "manifest_permission": True,
                                "delivery": "blocked",
                                "carries_candidate_data": True,
                                "candidate_data_kind": "location",
                                "location": sentinel_location,
                            },
                            {
                                "host": "job-boards.cdn.greenhouse.io",
                                "path": "/assets/flags-a2kmUSbF.webp",
                                "method": "GET",
                                "resource_type": "image",
                                "manifest_vendor": "greenhouse",
                                "manifest_permission": True,
                                "delivery": "blocked",
                                "carries_candidate_data": False,
                                "candidate_data_kind": "",
                            },
                            {
                                "host": "my.greenhouse.io",
                                "path": "/users" + "/self",
                                "method": "HEAD",
                                "resource_type": "fetch",
                                "manifest_vendor": "greenhouse",
                                "manifest_permission": True,
                                "delivery": "blocked",
                                "carries_candidate_data": False,
                                "candidate_data_kind": "",
                            },
                            {
                                "host": "job-boards.cdn.greenhouse.io",
                                "path": "/assets/flags-AlexSample1999.webp",
                                "method": "GET",
                                "resource_type": "image",
                                "manifest_vendor": "greenhouse",
                                "manifest_permission": True,
                                "delivery": "blocked",
                                "carries_candidate_data": False,
                                "candidate_data_kind": "",
                            },
                            {
                                **safe_email,
                                "path": f"/address/validate/{sentinel_email}",
                            },
                            {
                                **safe_email,
                                "path": "/address/validate/alex%2Eui%40example%2Etest",
                            },
                            {
                                "host": "unlisted.greenhouse.io",
                                "path": "/users" + "/self",
                                "method": "GET",
                                "resource_type": "fetch",
                                "manifest_vendor": "greenhouse",
                                "manifest_permission": True,
                                "delivery": "blocked",
                                "carries_candidate_data": False,
                                "candidate_data_kind": "",
                            },
                            {**safe_email, "resource_type": "image"},
                            {**safe_email, "candidate_data_kind": "location"},
                            {**safe_email, "manifest_vendor": "GreenHouse"},
                        ],
                    },
                ),
            )
            append_audit(
                session,
                AuditInput(
                    "automation",
                    "automation.other_run_finished",
                    "automation_run",
                    other_run.id,
                    {
                        "application_id": other_application_id,
                        "first_party_requests": [
                            {
                                "host": "other-application.greenhouse.io",
                                "path": "/must-not-display",
                                "method": "GET",
                                "resource_type": "fetch",
                                "manifest_vendor": "greenhouse",
                                "manifest_permission": True,
                                "delivery": "blocked",
                                "carries_candidate_data": False,
                                "candidate_data_kind": "",
                            }
                        ],
                    },
                ),
            )

        response = client.get(f"/applications/{application_id}")

    assert response.status_code == 200
    assert "Â·" not in response.text
    assert "First-party requests blocked locally" in response.text
    assert "application.evaluated" in response.text
    assert "automation.run_finished" in response.text
    assert "automation.other_run_finished" not in response.text
    assert "email-address-validator.us.greenhouse.io" in response.text
    assert response.text.count("<code>/address/validate</code>") == 2
    assert "api-geocode-earth-proxy.greenhouse.io" in response.text
    assert "<code>/v1/autocomplete</code>" in response.text
    assert "job-boards.cdn.greenhouse.io" in response.text
    assert "<code>/assets/flags-a2kmUSbF.webp</code>" in response.text
    assert "my.greenhouse.io" in response.text
    assert "<code>/users" + "/self</code>" in response.text
    assert "HEAD &middot; Resource type: fetch" in response.text
    assert "Candidate data: yes" in response.text
    assert "Kind: email" in response.text
    assert "Kind: location" in response.text
    assert "Candidate data: no" in response.text
    assert "Kind: none" in response.text
    for forbidden in (
        sentinel_email,
        sentinel_location,
        "ui-fragment",
        "?email=",
        "?text=",
        "?secret=",
        "must-not-display",
        "unlisted.greenhouse.io",
        "other-application.greenhouse.io",
        "x-location",
        "alex%2Eui%40example%2Etest",
        "AlexSample1999",
    ):
        assert forbidden not in response.text


@pytest.mark.parametrize("ui_v2", [False, True], ids=["v1", "v2"])
@pytest.mark.parametrize(
    "mismatch",
    ["current-application", "audit-page"],
)
def test_application_detail_rejects_cross_vendor_page_context(
    tmp_path: Path,
    ui_v2: bool,
    mismatch: str,
) -> None:
    with _client(tmp_path / mismatch / ("v2" if ui_v2 else "v1"), ui_v2=ui_v2) as client:
        application_id = _seed_application(
            client,
            employer="Cross Vendor UI Firm",
            state="NEEDS_USER",
        )
        with client.app.state.db.session_scope() as session:
            application = session.get(Application, application_id)
            assert application is not None
            opportunity = session.get(Opportunity, application.opportunity_id)
            assert opportunity is not None
            audit_page = "https://boards.greenhouse.io/acme/jobs/1234567"
            if mismatch == "current-application":
                opportunity.application_url = "https://jobs.lever.co/acme/role/apply"
                opportunity.resolved_ats_type = "lever"
            else:
                audit_page = "https://jobs.lever.co/acme/role/apply"
            run = AutomationRun(
                application_id=application_id,
                mode="prefill",
                state="NEEDS_USER",
                adapter="greenhouse",
            )
            session.add(run)
            session.flush()
            append_audit(
                session,
                AuditInput(
                    "automation",
                    "automation.run_finished",
                    "automation_run",
                    run.id,
                    {
                        "application_id": application_id,
                        "mode": "prefill",
                        "application_url": audit_page,
                        "first_party_requests": [_safe_email_request()],
                    },
                ),
            )

        response = client.get(f"/applications/{application_id}")

    assert response.status_code == 200
    assert "email-address-validator.us.greenhouse.io" not in response.text
    assert "<code>/address/validate</code>" not in response.text


@pytest.mark.parametrize("ui_v2", [False, True], ids=["v1", "v2"])
def test_application_detail_rejects_non_prefill_run_audits_without_execution(
    tmp_path: Path,
    ui_v2: bool,
) -> None:
    with _client(tmp_path / ("v2" if ui_v2 else "v1"), ui_v2=ui_v2) as client:
        application_id = _seed_application(
            client,
            employer="Non Prefill UI Firm",
            state="NEEDS_USER",
        )
        with client.app.state.db.session_scope() as session:
            for mode in ("inspect", "review", "submit"):
                run = AutomationRun(
                    application_id=application_id,
                    mode=mode,
                    state="NEEDS_USER",
                    adapter="greenhouse",
                )
                session.add(run)
                session.flush()
                append_audit(
                    session,
                    AuditInput(
                        "automation",
                        "automation.run_finished",
                        "automation_run",
                        run.id,
                        {
                            "application_id": application_id,
                            "mode": mode,
                            "application_url": "https://boards.greenhouse.io/acme/jobs/1234567",
                            "first_party_requests": [_safe_email_request()],
                        },
                    ),
                )

        response = client.get(f"/applications/{application_id}")

    assert response.status_code == 200
    assert "email-address-validator.us.greenhouse.io" not in response.text
    assert "<code>/address/validate</code>" not in response.text
