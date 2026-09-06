"""Phase 15 tests for the human-supplied application URL escape hatch.

Every browser-like target in this module is a loopback lab URL.  No test
contacts a public host or invokes submission authority.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.config import Settings
from app.domain.states import ApplicationState, UserApplicationStatus
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, AuditEvent, Opportunity
from app.services.target_resolution import ResolutionContext


LAB_SOURCE_URL = "http://127.0.0.1:8787/source/listing?id=phase15"
LAB_APPLICATION_URL = "http://127.0.0.1:8787/lab/ats/greenhouse/application"


class RecordingResolver:
    """A server-owned test resolver that records the one-call capability."""

    def __init__(
        self,
        *,
        kind: TargetKind = TargetKind.APPLICATION_ENTRY,
        employer: str | None = None,
        role: str | None = None,
    ) -> None:
        self.kind = kind
        self.employer = employer
        self.role = role
        self.calls: list[tuple[ResolutionContext, object, bool]] = []
        self.capability_was_active = False
        self.cross_host_was_refused = False
        self.same_origin_path_was_refused = False

    def __call__(
        self,
        context: ResolutionContext,
        *,
        source_capability: object | None = None,
        headed: bool = False,
    ) -> TargetResolution:
        self.calls.append((context, source_capability, headed))
        if source_capability is not None:
            self.capability_was_active = bool(source_capability.active)
            source_capability.assert_authorizes(context.inspection_url)
            with pytest.raises(ValueError):
                source_capability.assert_authorizes(
                    "http://127.0.0.2:8787/lab/ats/greenhouse/application"
                )
            with pytest.raises(ValueError):
                source_capability.assert_authorizes(
                    "http://127.0.0.1:8787/lab/ats/greenhouse/other"
                )
            self.cross_host_was_refused = True
            self.same_origin_path_was_refused = True
        evidence = {
            "synthetic_lab": True,
            "provider": "greenhouse",
            "application_origin": origin_for_url(context.inspection_url),
            "employer": self.employer if self.employer is not None else context.employer,
            "role": self.role if self.role is not None else context.role_title,
            "requisition": urlsplit(context.inspection_url).path,
            "form_identity": "greenhouse-application",
        }
        return TargetResolution(
            source_url=context.source_url,
            final_url=context.inspection_url,
            kind=self.kind,
            provider="greenhouse",
            identity_verified=self.kind
            not in {TargetKind.LISTING, TargetKind.AUTH_WALL},
            reason_codes=("phase15_test_observation",),
            evidence=evidence,
        )


def _client(tmp_path: Path, *, lab: bool = True) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    app.state.ui_v2_enabled = True
    app.state.manual_target_lab_enabled = lab
    return TestClient(app)


def _seed_blocked(
    client: TestClient,
    *,
    employer: str = "Phase 15 Employer",
    role: str = "Phase 15 Summer Analyst",
    user_status: UserApplicationStatus = UserApplicationStatus.NOT_APPLIED,
    target_status: TargetKind = TargetKind.BLOCKED,
    next_action: str = "Target resolution failed",
) -> Application:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=role,
            programme_group="summer",
            cycle="2027",
            location="London",
            url=LAB_SOURCE_URL,
            source="phase15_test",
            ats_type="greenhouse",
            user_status=user_status.value,
            target_status=target_status.value,
        )
        opportunity.application_window_status = "OPEN"
        opportunity.resolution_evidence_json = json.dumps(
            {
                "reason_codes": ["resolver_no_verified_result"],
                "next_action": next_action,
            }
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.BLOCKED.value,
            priority=70,
            risk_level=3,
            next_action=next_action,
            eligibility_json=json.dumps({"eligible": True, "reason_codes": []}),
            conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
        )
        session.add(application)
        session.flush()
        return application


def _audit_rows(client: TestClient, event_type: str) -> list[AuditEvent]:
    with client.app.state.db.session_scope() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.id.desc())
            ).all()
        )


def test_valid_human_url_promotes_exact_target_and_revokes_capability(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client)
        resolver = RecordingResolver()
        client.app.state.target_resolution_resolver = resolver
        before_allowlist = frozenset(
            getattr(client.app.state.settings, "live_domain_allowlist", frozenset())
        )

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I verified the exact employer application page.",
                "confirmed": True,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["human_supplied"] is True
        assert payload["promoted"] is True
        assert payload["target_kind"] == TargetKind.APPLICATION_ENTRY.value
        assert payload["application_url"] == LAB_APPLICATION_URL
        assert payload["submitted"] is False
        assert payload["application_advanced"] is True
        assert "Phase 15 Employer" in payload["message"]
        assert "Phase 15 Summer Analyst" in payload["message"]

        assert len(resolver.calls) == 1
        context, capability, headed = resolver.calls[0]
        assert context.application_id == application.id
        assert context.inspection_url == LAB_APPLICATION_URL
        assert headed is False
        assert capability is not None
        assert resolver.capability_was_active is True
        assert resolver.cross_host_was_refused is True
        assert resolver.same_origin_path_was_refused is True
        assert capability.hostname == "127.0.0.1"
        assert capability.origin == origin_for_url(LAB_APPLICATION_URL)
        assert capability.active is False
        with pytest.raises(ValueError, match="revoked"):
            capability.assert_authorizes(LAB_APPLICATION_URL)
        with pytest.raises(ValueError, match="revoked"):
            capability.assert_authorizes_navigation("http://127.0.0.2:8787/other")

        assert frozenset(
            getattr(client.app.state.settings, "live_domain_allowlist", frozenset())
        ) == before_allowlist

    rows = _audit_rows(client, "opportunity.target_resolution_human_supplied")
    assert rows
    details = json.loads(rows[0].details_json)
    assert rows[0].actor == "user"
    assert details["actor"] == "user"
    assert details["input_source"] == "human_supplied_application_url"
    assert details["typed_url"] == LAB_APPLICATION_URL
    assert details["typed_at"]
    assert details["verification_outcome"]["status"] == "verified"
    assert details["submitted"] is False
    assert details["capability"]["hostname"] == "127.0.0.1"
    assert details["capability"]["revoked"] is True


def test_different_employer_is_rejected_and_application_stays_blocked(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client, employer="Correct Employer")
        resolver = RecordingResolver(employer="Different Employer")
        client.app.state.target_resolution_resolver = resolver

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I checked the target identity before retrying.",
                "confirmed": True,
            },
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["promoted"] is False
        assert payload["application_advanced"] is False
        assert payload["submitted"] is False
        assert "employer" in payload["verification"]["failed_check"]
        assert "mismatch" in payload["verification"]["failed_check"]
        assert payload["state"] == ApplicationState.BLOCKED.value
        assert payload["automation_url"] is None

        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.state == ApplicationState.BLOCKED.value
            opportunity = session.get(Opportunity, persisted.opportunity_id)
            assert opportunity is not None
            assert opportunity.automation_url is None
            assert opportunity.target_status == TargetKind.UNRESOLVED.value
            assert persisted.next_action == "Target verification failed: manual_employer_mismatch"

    rows = _audit_rows(client, "opportunity.target_resolution_human_supplied")
    details = json.loads(rows[0].details_json)
    assert details["typed_url"] == LAB_APPLICATION_URL
    assert details["verification_outcome"]["status"] == "rejected"
    assert "employer" in details["verification_outcome"]["failed_check"]


def test_failed_manual_url_is_not_retained_as_unverified_application_target(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client, employer="Correct Employer")
        client.app.state.target_resolution_resolver = RecordingResolver(
            employer="Different Employer"
        )

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I checked the target identity before retrying.",
                "confirmed": True,
            },
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["state"] == ApplicationState.BLOCKED.value
        assert payload["application_url"] is None
        assert payload["automation_url"] is None

        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.state == ApplicationState.BLOCKED.value
            opportunity = session.get(Opportunity, persisted.opportunity_id)
            assert opportunity is not None
            assert opportunity.application_url is None
            assert opportunity.automation_url is None
            assert opportunity.target_status == TargetKind.UNRESOLVED.value


def test_resolver_failure_clears_staged_url_and_keeps_blocked_state(
    tmp_path: Path,
) -> None:
    class ExplodingResolver:
        def __call__(self, _context: ResolutionContext) -> TargetResolution:
            raise RuntimeError("synthetic resolver failure")

    with _client(tmp_path) as client:
        application = _seed_blocked(client)
        client.app.state.target_resolution_resolver = ExplodingResolver()

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I checked the target identity before retrying.",
                "confirmed": True,
            },
        )

        assert response.status_code == 202
        assert response.json()["verification"]["failed_check"] == "resolver_failed"
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.state == ApplicationState.BLOCKED.value
            opportunity = session.get(Opportunity, persisted.opportunity_id)
            assert opportunity is not None
            assert opportunity.application_url is None
            assert opportunity.automation_url is None

    rows = _audit_rows(client, "opportunity.target_resolution_human_supplied")
    details = json.loads(rows[0].details_json)
    assert details["typed_url"] == LAB_APPLICATION_URL
    assert details["verification_outcome"]["failed_check"] == "resolver_failed"
    assert details["capability"]["revoked"] is True


def test_status_change_during_resolution_clears_staged_url(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client)

        class StatusChangingResolver:
            def __call__(self, context: ResolutionContext) -> TargetResolution:
                with client.app.state.db.session_scope() as other_session:
                    opportunity = other_session.get(Opportunity, application.opportunity_id)
                    assert opportunity is not None
                    opportunity.user_status = UserApplicationStatus.APPLICATION_SUBMITTED.value
                return RecordingResolver()(context)

        client.app.state.target_resolution_resolver = StatusChangingResolver()
        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I checked the target identity before retrying.",
                "confirmed": True,
            },
        )

        assert response.status_code == 409
        assert "APPLICATION_SUBMITTED" in response.json()["detail"]
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.state == ApplicationState.BLOCKED.value
            opportunity = session.get(Opportunity, persisted.opportunity_id)
            assert opportunity is not None
            assert opportunity.user_status == UserApplicationStatus.APPLICATION_SUBMITTED.value
            assert opportunity.application_url is None


def test_different_role_is_rejected_before_target_promotion(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client, role="Correct Role")
        client.app.state.target_resolution_resolver = RecordingResolver(role="Different Role")

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I checked the role identity before retrying.",
                "confirmed": True,
            },
        )

        assert response.status_code == 202
        assert response.json()["verification"]["failed_check"] == "manual_role_mismatch"
        assert response.json()["state"] == ApplicationState.BLOCKED.value
        assert response.json()["submitted"] is False


def test_manual_url_validation_is_https_public_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import api

    monkeypatch.setattr(api, "safe_public_navigation_url", lambda _url: True)
    assert api.validate_human_supplied_application_url(
        "HTTPS://Example.COM:443/jobs/123?source=human#form"
    ) == "https://example.com:443/jobs/123?source=human#form"
    assert api.validate_human_supplied_application_url(
        LAB_APPLICATION_URL,
        allow_loopback=True,
    ) == LAB_APPLICATION_URL

    invalid = (
        "http://example.com/application",
        "https://example.com:8443/application",
        "https://user:password@example.com/application",
        "https://localhost/application",
        "https://127.0.0.2/application",
        "https://example.com/application with space",
        "https://example.com/\napplication",
        "file:///candidate/application.html",
        "javascript:alert(1)",
        "data:text/html,application",
    )
    for value in invalid:
        with pytest.raises(ValueError):
            api.validate_human_supplied_application_url(value)
    with pytest.raises(ValueError, match="too_long"):
        api.validate_human_supplied_application_url("https://example.com/" + "x" * 2048)


@pytest.mark.parametrize("kind", [TargetKind.LISTING, TargetKind.AUTH_WALL])
def test_listing_or_auth_wall_never_promotes_human_url(
    tmp_path: Path, kind: TargetKind
) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client, target_status=kind)
        resolver = RecordingResolver(kind=kind)
        client.app.state.target_resolution_resolver = resolver

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I reviewed the page classification carefully.",
                "confirmed": True,
            },
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["promoted"] is False
        assert payload["target_kind"] == kind.value
        assert payload["application_advanced"] is False
        assert payload["submitted"] is False
        assert payload["state"] == ApplicationState.BLOCKED.value


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787/lab/ats/greenhouse/application",
        "https://127.0.0.1:8443/lab/ats/greenhouse/application",
        "https://user:password@example.com/application",
        "https://example.com:8443/application",
        "file:///C:/candidate/application.html",
        "javascript:alert(1)",
        "data:text/html,application",
        "https://private.local/application",
        "https://192.168.1.10/application",
    ],
)
def test_invalid_human_url_is_rejected_before_resolver(tmp_path: Path, url: str) -> None:
    with _client(tmp_path, lab=False) as client:
        application = _seed_blocked(client)
        resolver = RecordingResolver()
        client.app.state.target_resolution_resolver = resolver

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": url,
                "resolution_reason": "I supplied a URL for target verification.",
                "confirmed": True,
            },
        )

        assert response.status_code == 422
        assert resolver.calls == []
        assert response.json()["submitted"] is False

    rows = _audit_rows(client, "opportunity.target_resolution_human_supplied")
    assert rows
    details = json.loads(rows[0].details_json)
    assert details["typed_url_present"] is True
    assert details["typed_url"] == ""
    assert details["verification_outcome"]["status"] == "rejected"


def test_target_blocker_without_url_explains_and_preserves_blocked_state(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client)
        resolver = RecordingResolver()
        client.app.state.target_resolution_resolver = resolver

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "resolution_reason": "I reviewed the recorded target failure.",
                "confirmed": True,
            },
        )

        assert response.status_code == 409
        assert "real application URL" in response.json()["detail"]
        assert resolver.calls == []
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            assert persisted.state == ApplicationState.BLOCKED.value


@pytest.mark.parametrize(
    "status",
    [
        UserApplicationStatus.NOT_INTERESTED,
        UserApplicationStatus.APPLICATION_SUBMITTED,
        UserApplicationStatus.ONLINE_ASSESSMENT,
        UserApplicationStatus.HIREVUE,
        UserApplicationStatus.ONLINE_TEST,
        UserApplicationStatus.FIRST_ROUND,
        UserApplicationStatus.OFFER,
        UserApplicationStatus.REJECTED,
    ],
)
def test_excluded_user_status_cannot_accept_manual_url(
    tmp_path: Path, status: UserApplicationStatus
) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(
            client,
            user_status=status,
        )
        resolver = RecordingResolver()
        client.app.state.target_resolution_resolver = resolver

        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I checked the current candidate-owned status.",
                "confirmed": True,
            },
        )

        assert response.status_code == 409
        assert status.value in response.json()["detail"]
        assert resolver.calls == []

        page = client.get("/needs-you?group=blocked")
        assert page.status_code == 200
        assert status.value.replace("_", " ").title() in page.text
        assert "No action required" in page.text
        assert f'name="application_url"' not in page.text


def test_v2_target_blocker_has_url_field_and_relabelled_action(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_blocked(client)

        page = client.get("/needs-you?group=blocked")

        assert page.status_code == 200
        assert f'action="/api/applications/{application.id}/resolve-blocker"' in page.text
        assert 'name="application_url"' in page.text
        assert 'name="resolution_reason"' in page.text
        assert "Verify URL &amp; rerun checks" in page.text
        assert "Record resolution &amp; rerun checks" not in page.text
        assert "No action available yet without target verification" in page.text


def test_manual_payload_cannot_select_submit_mode(tmp_path: Path) -> None:
    from app.routers.api import resolve_application_blocker

    source = inspect.getsource(resolve_application_blocker)
    assert "RunMode.SUBMIT" not in source
    assert "AutomationRunner" not in source

    with _client(tmp_path) as client:
        application = _seed_blocked(client)
        resolver = RecordingResolver()
        client.app.state.target_resolution_resolver = resolver
        response = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "application_url": LAB_APPLICATION_URL,
                "resolution_reason": "I verified the exact page and role identity.",
                "confirmed": True,
                "mode": "submit",
            },
        )

        assert response.status_code == 422
        assert resolver.calls == []
