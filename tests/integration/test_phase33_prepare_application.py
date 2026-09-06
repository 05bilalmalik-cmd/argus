"""Phase 33 candidate-started PREFILL preparation contracts.

All HTTP assertions use temporary FastAPI ``TestClient`` applications. The
fixtures contain synthetic employer/role values and no candidate data.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.automation.host_policy import origin_for_url
from app.automation.runner import AutomationRunner
from app.automation.targets import TargetResolution
from app.automation.types import AutomationOutcome, RunMode, SessionSnapshot, SessionState
from app.config import Settings
from app.domain.states import ApplicationState, UserApplicationStatus
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, AuditEvent, AutomationRun, Opportunity
from app.security.audit import AuditInput, append_audit
from app.services.navigator import ApplicationNavigator


def _client(tmp_path: Path) -> TestClient:
    app = create_app(
        Settings.load(
            {
                "ARGUS_DATA_DIR": str(tmp_path),
                "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
                "ARGUS_ENABLE_NOTIFICATIONS": "false",
            }
        )
    )
    app.state.ui_v2_enabled = True
    return TestClient(app)


def _seed(
    client: TestClient,
    application_id: str,
    *,
    target_status: TargetKind = TargetKind.APPLICATION_FORM,
    application_state: ApplicationState = ApplicationState.NEEDS_USER,
    user_status: UserApplicationStatus = UserApplicationStatus.NOT_APPLIED,
    cv_required: bool = False,
    application_url: str | None = None,
) -> str:
    target_url = application_url or f"https://apply.example.test/{application_id}/form"
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            id=f"opportunity-{application_id}",
            employer="Example Employer",
            role_title="Example Role",
            programme_group="summer",
            cycle="2027",
            location="London",
            url=f"https://source.example.test/{application_id}",
            application_url=target_url if target_status is TargetKind.APPLICATION_FORM else application_url,
            source="phase33_test",
            ats_type="generic" if target_status is TargetKind.APPLICATION_FORM else "",
            target_status=target_status.value,
            resolved_ats_type="generic" if target_status is TargetKind.APPLICATION_FORM else "",
            resolved_at=(datetime.now(timezone.utc) if target_status is TargetKind.APPLICATION_FORM else None),
            application_window_status="OPEN",
            user_status=user_status.value,
            user_status_actor="test",
            cv_required=cv_required,
            resolution_evidence_json=json.dumps(
                {
                    "reason_codes": ["phase33_test_target"],
                    "provider": "generic",
                    "application_origin": origin_for_url(target_url),
                }
            ),
        )
        application = Application(
            id=application_id,
            opportunity=opportunity,
            state=application_state.value,
            priority=50,
            next_action="Review this application",
            eligibility_json=json.dumps({"eligible": True, "reason_codes": []}),
            conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
        )
        session.add(application)
    return application_id


def _snapshot(application_id: str, *, state: SessionState = SessionState.HUMAN_REQUIRED) -> SessionSnapshot:
    now = datetime.now(timezone.utc)
    return SessionSnapshot(
        session_id=f"live-{application_id}",
        application_id=application_id,
        mode=RunMode.PREFILL.value,
        state=state,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        reason="CAPTCHA requires candidate action",
        summary={},
        worker_alive=True,
        cleanup_complete=False,
        headed=True,
        human_boundary={
            "kind": "captcha",
            "reason": "CAPTCHA requires candidate action",
            "can_continue": True,
            "can_cancel": True,
            "resumable": True,
        },
        resumable=True,
    )


def _outcome(application_id: str, *, session_id: str = "session-phase33", run_id: str = "") -> AutomationOutcome:
    return AutomationOutcome(
        state=ApplicationState.NEEDS_USER.value,
        risk_level=3,
        adapter="generic",
        blocked_reasons=("human_boundary",),
        run_id=run_id,
        application_url=f"https://apply.example.test/{application_id}/form",
        target_status=TargetKind.APPLICATION_FORM.value,
        handoff_session_id=session_id,
        human_boundary={
            "kind": "captcha",
            "reason": "CAPTCHA requires candidate action",
            "can_continue": True,
            "can_cancel": True,
        },
    )


def test_candidate_start_is_constant_headed_prefill_and_never_submit(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str, RunMode, bool]] = []

    def fake_run(self, application_id, mode, headed=False, session_id="", authority_id=""):  # noqa: ANN001
        calls.append((application_id, mode, headed))
        return _outcome(application_id)

    monkeypatch.setattr(AutomationRunner, "run", fake_run)
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "prefill-start",
            application_state=ApplicationState.PACKAGE_PREPARED,
        )
        response = client.post(
            f"/api/applications/{application_id}/prefill?mode=submit",
            json={"mode": "submit"},
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["mode"] == RunMode.PREFILL.value
    assert payload["headed"] is True
    assert payload["submitted"] is False
    assert payload["click_boundary_crossed"] is False
    assert payload["session_id"] == "session-phase33"
    assert calls == [(application_id, RunMode.PREFILL, True)]


def test_excluded_status_has_no_prepare_control_and_refuses_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_run(self, application_id, mode, headed=False, session_id="", authority_id=""):  # noqa: ANN001
        calls.append(application_id)
        return _outcome(application_id)

    monkeypatch.setattr(AutomationRunner, "run", fake_run)
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "excluded-status",
            application_state=ApplicationState.PACKAGE_PREPARED,
            user_status=UserApplicationStatus.NOT_INTERESTED,
        )
        detail = client.get(f"/applications/{application_id}")
        needs = client.get("/needs-you?group=blocked")
        response = client.post(f"/api/applications/{application_id}/prefill")

    reason = "User status NOT_INTERESTED excludes this opportunity from automation"
    assert detail.status_code == 200
    assert reason in detail.text
    assert 'data-prefill-start' not in detail.text
    assert "Prepare this application" not in detail.text
    assert needs.status_code == 200
    assert 'data-prefill-start' not in needs.text
    assert response.status_code == 409
    assert response.json()["code"] == "user_status_excluded"
    assert reason in response.json()["wall"]
    assert calls == []


def test_prefill_refuses_non_application_form_and_names_actual_target(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_run(self, application_id, mode, headed=False, session_id="", authority_id=""):  # noqa: ANN001
        calls.append(application_id)
        return _outcome(application_id)

    monkeypatch.setattr(AutomationRunner, "run", fake_run)
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "job-detail-target",
            target_status=TargetKind.JOB_DETAIL,
        )
        detail = client.get(f"/applications/{application_id}")
        response = client.post(f"/api/applications/{application_id}/prefill")

    assert detail.status_code == 200
    assert 'data-prefill-start' not in detail.text
    assert response.status_code == 409
    assert response.json()["code"] == "target_not_application_form"
    assert TargetKind.JOB_DETAIL.value in response.json()["wall"]
    assert calls == []


def test_live_session_refusal_offers_exact_continue_and_detail_has_no_stale_prepare(
    tmp_path: Path, monkeypatch
) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(client, "live-session", application_state=ApplicationState.NEEDS_USER)
        live = _snapshot(application_id)
        monkeypatch.setattr(
            client.app.state.navigator,
            "active_for_application",
            lambda _application_id: live,
        )

        response = client.post(f"/api/applications/{application_id}/prefill")
        detail = client.get(f"/applications/{application_id}")
        needs = client.get("/needs-you?group=review")

    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "prefill_session_active"
    assert payload["continue"]["session_id"] == live.session_id
    assert detail.status_code == 200
    assert 'data-navigator-continue' in detail.text
    assert f'data-session-id="{live.session_id}"' in detail.text
    assert 'data-prefill-start' not in detail.text
    assert needs.status_code == 200
    assert f'href="/applications/{application_id}"' in needs.text
    assert "Continue" in needs.text


def test_concurrent_double_start_creates_one_run_and_second_request_gets_continue(
    tmp_path: Path, monkeypatch
) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "double-start",
            application_state=ApplicationState.PACKAGE_PREPARED,
        )
        live_holder: dict[str, SessionSnapshot | None] = {"snapshot": None}
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        monkeypatch.setattr(
            client.app.state.navigator,
            "active_for_application",
            lambda _application_id: live_holder["snapshot"],
        )

        def fake_run(self, requested_id, mode, headed=False, session_id="", authority_id=""):  # noqa: ANN001
            calls.append(requested_id)
            live_holder["snapshot"] = _snapshot(requested_id)
            entered.set()
            assert release.wait(5)
            return _outcome(requested_id, session_id=live_holder["snapshot"].session_id)

        monkeypatch.setattr(AutomationRunner, "run", fake_run)
        responses: list[object] = []

        first_thread = threading.Thread(
            target=lambda: responses.append(client.post(f"/api/applications/{application_id}/prefill")),
        )
        first_thread.start()
        assert entered.wait(5)
        second_thread = threading.Thread(
            target=lambda: responses.append(client.post(f"/api/applications/{application_id}/prefill")),
        )
        second_thread.start()
        release.set()
        first_thread.join(10)
        second_thread.join(10)

    assert len(responses) == 2
    assert sorted(response.status_code for response in responses) == [202, 409]
    assert calls == [application_id]
    duplicate = next(response for response in responses if response.status_code == 409)
    assert duplicate.json()["continue"]["session_id"] == f"live-{application_id}"


def test_missing_cv_returns_exact_required_cv_wall_without_opening_browser(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_run(self, application_id, mode, headed=False, session_id="", authority_id=""):  # noqa: ANN001
        calls.append(application_id)
        return _outcome(application_id)

    monkeypatch.setattr(AutomationRunner, "run", fake_run)
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "missing-cv",
            cv_required=True,
            application_state=ApplicationState.NEEDS_USER,
        )
        response = client.post(f"/api/applications/{application_id}/prefill")

    assert response.status_code == 409
    assert response.json()["code"] == "required_cv_missing"
    assert "required_cv_missing" in response.json()["wall"]
    assert calls == []


def test_egress_failure_wall_has_sanitized_host_classification(
    tmp_path: Path, monkeypatch
) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "egress-wall",
            application_state=ApplicationState.PACKAGE_PREPARED,
        )
        with client.app.state.db.session_scope() as session:
            run = AutomationRun(
                application_id=application_id,
                mode=RunMode.PREFILL.value,
                state=ApplicationState.NEEDS_USER.value,
                error="Filling stopped; one functional request was blocked",
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
                        "mode": RunMode.PREFILL.value,
                        "blocked_requests": [
                            {
                                "host": "blocked.example.test",
                                "path": "/collect?candidate=value",
                                "classification": "data_bearing",
                                "impact": "fatal",
                                "reason": "nonpassive_cross_origin",
                            }
                        ],
                    },
                ),
            )
            run_id = run.id

        monkeypatch.setattr(
            AutomationRunner,
            "run",
            lambda self, requested_id, mode, headed=False, session_id="", authority_id="": _outcome(
                requested_id, session_id="", run_id=run_id
            ),
        )
        response = client.post(f"/api/applications/{application_id}/prefill")

    assert response.status_code == 202
    payload = response.json()
    assert "blocked.example.test" in payload["wall"]
    assert "data_bearing" in payload["wall"]
    assert "candidate=value" not in payload["wall"]


def test_start_audit_records_user_actor_and_prefill_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        AutomationRunner,
        "run",
        lambda self, application_id, mode, headed=False, session_id="", authority_id="": _outcome(
            application_id
        ),
    )
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "audited-start",
            application_state=ApplicationState.PACKAGE_PREPARED,
        )
        response = client.post(f"/api/applications/{application_id}/prefill")
        with client.app.state.db.session_scope() as session:
            events = list(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == "application.prefill_started")
                ).all()
            )

    assert response.status_code == 202
    assert len(events) == 1
    assert events[0].actor == "user"
    details = json.loads(events[0].details_json)
    assert details["mode"] == RunMode.PREFILL.value
    assert details["headed"] is True
    assert "employer" not in details
    assert "role" not in details


def test_detail_offers_prepare_without_live_continue_and_needs_row_has_prepare(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "prepare-control",
            application_state=ApplicationState.PACKAGE_PREPARED,
        )
        detail = client.get(f"/applications/{application_id}")
        needs_id = _seed(client, "needs-row-prepare")
        needs = client.get("/needs-you?group=review")

    assert detail.status_code == 200
    assert 'data-prefill-start' in detail.text
    assert "Prepare this application" in detail.text
    assert 'data-navigator-continue' not in detail.text
    assert needs.status_code == 200
    assert f'data-application-id="{needs_id}"' in needs.text
    assert "Prepare this application" in needs.text


def test_in_progress_filling_row_is_visible_as_review_with_continue(
    tmp_path: Path, monkeypatch
) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "filling-row",
            application_state=ApplicationState.FILLING,
        )
        live = _snapshot(application_id, state=SessionState.ACTIVE)
        monkeypatch.setattr(
            client.app.state.navigator,
            "active_for_application",
            lambda _application_id: live,
        )
        needs = client.get("/needs-you?group=review")

    assert needs.status_code == 200
    assert f'data-application-id="{application_id}"' in needs.text
    assert "Preparing" in needs.text or "Continue" in needs.text


def test_phase15_blocked_bucket_keeps_human_url_escape_hatch(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            "blocked-escape",
            target_status=TargetKind.BLOCKED,
            application_state=ApplicationState.BLOCKED,
            application_url=None,
        )
        with client.app.state.db.session_scope() as session:
            opportunity = session.get(Opportunity, f"opportunity-{application_id}")
            opportunity.resolution_evidence_json = json.dumps(
                {"reason_codes": ["resolver_no_verified_result"]}
            )
        page = client.get("/needs-you?group=blocked")

    assert page.status_code == 200
    assert f"/api/applications/{application_id}/resolve-blocker" in page.text
    assert 'data-manual-target-form' in page.text
    assert 'name="application_url" type="url"' in page.text
    assert "Verify URL &amp; rerun checks" in page.text
    assert 'data-prefill-start' not in page.text


def test_row_allowlist_is_run_scoped_and_does_not_change_settings() -> None:
    captured: list[dict[str, object]] = []

    class CaptureWorker:
        owner_thread_id = None
        cleanup_complete = True

        def __init__(self, **kwargs):  # noqa: ANN003
            captured.append(kwargs)

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout: float | None = None) -> None:
            return None

    settings = SimpleNamespace(
        browser_headless=True,
        live_domain_allowlist=frozenset({"configured.example.test"}),
        apply_click_enabled=False,
        role_match_v2_enabled=False,
        egress_impact_classification_enabled=True,
        cleanup_grace_seconds=1,
    )
    before = settings.live_domain_allowlist
    navigator = ApplicationNavigator(settings=settings, worker_factory=CaptureWorker)
    url = "https://row.example.test/application/42"
    resolution = TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_FORM,
        provider="generic",
        identity_verified=True,
        form_verified=True,
        evidence={
            "provider": "generic",
            "application_origin": origin_for_url(url),
            "employer": "Example Employer",
            "role": "Example Role",
            "requisition": "/application/42",
            "form_identity": "42",
        },
    )

    snapshot = navigator.start(
        "allowlist-application",
        RunMode.PREFILL,
        headed=True,
        resolution=resolution,
        run_allowlist=frozenset({"row.example.test"}),
    )
    navigator.shutdown()

    assert snapshot.mode == RunMode.PREFILL.value
    assert captured[0]["allowlist"] == frozenset({"row.example.test"})
    assert captured[0]["allowlist_is_run_scoped"] is True
    assert settings.live_domain_allowlist == before
