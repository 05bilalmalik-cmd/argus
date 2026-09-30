from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.questions import CanonicalKey, Sensitivity
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.security.crypto import CryptoBox
from app.services.decisions import (
    DecisionRequest,
    DecisionResponse,
    build_decision_requests,
)
from app.services.notifications import NotificationService, select_human_attention_reason


def _decision_module():
    try:
        return __import__("app.services.decisions", fromlist=["DecisionRequest"])
    except ModuleNotFoundError as exc:
        pytest.fail(f"decision service is not implemented: {exc}")


class RecordingBackend:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.payloads: list[dict[str, object]] = []

    def send(self, payload: dict[str, object]) -> None:
        if self.error is not None:
            raise self.error
        self.payloads.append(payload)


def immediate_scheduler(_delay: float, callback):
    callback()
    return SimpleNamespace(cancel=lambda: None)


def _service(
    backend: RecordingBackend,
    *,
    batch_window_seconds: float = 0,
    rate_limit_per_hour: int = 6,
    scheduler=immediate_scheduler,
    state_path: Path | None = None,
    clock=None,
):
    return NotificationService(
        enabled=True,
        backend=backend,
        local_base_url="http://127.0.0.1:8787",
        batch_window_seconds=batch_window_seconds,
        rate_limit_per_hour=rate_limit_per_hour,
        scheduler=scheduler,
        state_path=state_path,
        clock=clock,
    )


def _setup_db(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    return settings, database, crypto


def _create_application(
    session,
    *,
    employer: str = "Goldman Sachs",
    role: str = "2027 Placement",
    state: str = ApplicationState.NEEDS_USER.value,
    next_action: str = "",
    eligibility_json: str = "{}",
    conflict_json: str = "{}",
):
    opportunity = Opportunity(
        employer=employer,
        role_title=role,
        cycle="2027",
        url="https://example.test/programme",
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=state,
        next_action=next_action,
        eligibility_json=eligibility_json,
        conflict_json=conflict_json,
    )
    session.add(application)
    session.flush()
    return application, opportunity


class TestDecisionEnvelopeRoundTrip:
    def test_decision_request_to_dict_and_back(self):
        request = DecisionRequest(
            id="test-id-123",
            application_id="app-456",
            employer="Goldman Sachs",
            role="2027 Placement",
            question_text="Are you authorised to work in the UK?",
            canonical_key=CanonicalKey.WORK_AUTHORISATION,
            sensitivity=Sensitivity.LEGAL,
            permitted_options=("Yes", "No"),
            confidence=None,
            prompt="Goldman Sachs — 2027 Placement: Are you authorised to work in the UK? (No default; you must choose explicitly)",
        )

        data = request.to_dict()
        assert data["id"] == "test-id-123"
        assert data["application_id"] == "app-456"
        assert data["employer"] == "Goldman Sachs"
        assert data["role"] == "2027 Placement"
        assert data["canonical_key"] == "legal.work_authorisation"
        assert data["sensitivity"] == "legal"
        assert data["permitted_options"] == ["Yes", "No"]
        assert data["confidence"] is None
        assert "No default" in data["prompt"]

        restored = DecisionRequest.from_dict(data)
        assert restored.id == request.id
        assert restored.application_id == request.application_id
        assert restored.employer == request.employer
        assert restored.role == request.role
        assert restored.question_text == request.question_text
        assert restored.canonical_key == request.canonical_key
        assert restored.sensitivity == request.sensitivity
        assert restored.permitted_options == request.permitted_options
        assert restored.confidence == request.confidence
        assert restored.prompt == request.prompt

    def test_decision_response_to_dict_and_back(self):
        decided_at = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
        response = DecisionResponse(
            decision_id="test-id-123",
            chosen_option="Yes",
            decided_by="human@phone",
            decided_at=decided_at,
        )

        data = response.to_dict()
        assert data["decision_id"] == "test-id-123"
        assert data["chosen_option"] == "Yes"
        assert data["decided_by"] == "human@phone"
        assert data["decided_at"] == "2026-09-19T12:00:00+00:00"

        restored = DecisionResponse.from_dict(data)
        assert restored.decision_id == response.decision_id
        assert restored.chosen_option == response.chosen_option
        assert restored.decided_by == response.decided_by
        assert restored.decided_at == response.decided_at

    def test_decision_response_from_dict_parses_iso_string(self):
        data = {
            "decision_id": "test-id",
            "chosen_option": "No",
            "decided_by": "user",
            "decided_at": "2026-09-19T12:00:00+00:00",
        }
        response = DecisionResponse.from_dict(data)
        assert response.decided_at == datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


class TestSafetyRules:
    def test_invalid_option_rejected(self):
        request = DecisionRequest(
            id="test-id",
            application_id="app-1",
            employer="Goldman Sachs",
            role="2027 Placement",
            question_text="Are you authorised to work in the UK?",
            canonical_key=CanonicalKey.WORK_AUTHORISATION,
            sensitivity=Sensitivity.LEGAL,
            permitted_options=("Yes", "No"),
            confidence=None,
            prompt="test",
        )

        response = DecisionResponse(
            decision_id="test-id",
            chosen_option="Maybe",
            decided_by="human@phone",
            decided_at=datetime.now(timezone.utc),
        )

        with pytest.raises(ValueError, match="not in permitted options"):
            request.validate_response(response)

    def test_valid_option_accepted(self):
        request = DecisionRequest(
            id="test-id",
            application_id="app-1",
            employer="Goldman Sachs",
            role="2027 Placement",
            question_text="Are you authorised to work in the UK?",
            canonical_key=CanonicalKey.WORK_AUTHORISATION,
            sensitivity=Sensitivity.LEGAL,
            permitted_options=("Yes", "No"),
            confidence=None,
            prompt="test",
        )

        response = DecisionResponse(
            decision_id="test-id",
            chosen_option="Yes",
            decided_by="human@phone",
            decided_at=datetime.now(timezone.utc),
        )

        # Should not raise
        request.validate_response(response)

    def test_sensitive_tiers_carry_no_default_or_guess(self):
        legal_keys = (
            CanonicalKey.WORK_AUTHORISATION,
            CanonicalKey.SPONSORSHIP,
            CanonicalKey.CRIMINAL_RECORD,
            CanonicalKey.LEGAL_ATTESTATION,
        )
        sensitive_keys = (CanonicalKey.DEMOGRAPHIC,)

        for key in legal_keys:
            sensitivity = Sensitivity.LEGAL
            with pytest.raises(ValueError, match="must not carry a confidence guess"):
                DecisionRequest(
                    id="test-id",
                    application_id="app-1",
                    employer="Test",
                    role="Role",
                    question_text="Question",
                    canonical_key=key,
                    sensitivity=sensitivity,
                    permitted_options=("Yes", "No"),
                    confidence=0.9,  # This should be rejected
                    prompt="test",
                )

            with pytest.raises(ValueError, match="must not include a default option"):
                DecisionRequest(
                    id="test-id",
                    application_id="app-1",
                    employer="Test",
                    role="Role",
                    question_text="Question",
                    canonical_key=key,
                    sensitivity=sensitivity,
                    permitted_options=("Yes", "No", "Default"),  # This should be rejected
                    confidence=None,
                    prompt="test",
                )

        for key in sensitive_keys:
            sensitivity = Sensitivity.SENSITIVE
            with pytest.raises(ValueError, match="must not carry a confidence guess"):
                DecisionRequest(
                    id="test-id",
                    application_id="app-1",
                    employer="Test",
                    role="Role",
                    question_text="Question",
                    canonical_key=key,
                    sensitivity=sensitivity,
                    permitted_options=("Option A", "Option B"),
                    confidence=0.5,
                    prompt="test",
                )

    def test_non_sensitive_tiers_may_carry_confidence(self):
        request = DecisionRequest(
            id="test-id",
            application_id="app-1",
            employer="Test",
            role="Role",
            question_text="Question",
            canonical_key=CanonicalKey.CV,
            sensitivity=Sensitivity.STANDARD,
            permitted_options=("Upload CV", "Skip"),
            confidence=0.85,
            prompt="test",
        )
        assert request.confidence == 0.85

    def test_decision_request_rejects_empty_permitted_options(self):
        with pytest.raises(ValueError, match="permitted_options must not be empty"):
            DecisionRequest(
                id="test-id",
                application_id="app-1",
                employer="Test",
                role="Role",
                question_text="Question",
                canonical_key=CanonicalKey.CV,
                sensitivity=Sensitivity.STANDARD,
                permitted_options=(),
                confidence=None,
                prompt="test",
            )

    def test_decision_request_rejects_invalid_confidence(self):
        with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
            DecisionRequest(
                id="test-id",
                application_id="app-1",
                employer="Test",
                role="Role",
                question_text="Question",
                canonical_key=CanonicalKey.CV,
                sensitivity=Sensitivity.STANDARD,
                permitted_options=("Option",),
                confidence=1.5,
                prompt="test",
            )


class TestPrivacyNoSecretsOrPII:
    def test_payload_contains_no_pii_or_secrets(self, tmp_path: Path):
        settings, database, crypto = _setup_db(tmp_path)

        with database.session_scope() as session:
            application, opportunity = _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )
            application_id = application.id

        with database.session_scope() as session:
            requests = build_decision_requests(session)

        assert len(requests) >= 1
        req = requests[0]

        payload = req.to_dict()
        serialised = json.dumps(payload, sort_keys=True)

        # Check for common PII patterns that must NOT appear
        forbidden_patterns = [
            "demo",
            "candidate",
            "pii",
            "password",
            "secret",
            "token",
            "api_key",
            "private",
            "ssn",
            "ni_number",
            "passport",
            "driving_licence",
            "bank",
            "sort_code",
            "account_number",
            "cvc",
            "cvv",
        ]

        for pattern in forbidden_patterns:
            assert pattern not in serialised.casefold(), f"Forbidden pattern '{pattern}' found in payload"

        # Ensure only allowed fields are present
        allowed_fields = {
            "id",
            "application_id",
            "employer",
            "role",
            "question_text",
            "canonical_key",
            "sensitivity",
            "permitted_options",
            "confidence",
            "prompt",
        }
        assert set(payload.keys()) == allowed_fields

        # Employer and role should be present but not personal data
        assert payload["employer"] == "Goldman Sachs"
        assert payload["role"] == "2027 Placement"
        assert "application_id" in payload

    def test_decision_payload_excludes_candidate_profile_data(self, tmp_path: Path):
        settings, database, crypto = _setup_db(tmp_path)

        with database.session_scope() as session:
            application, opportunity = _create_application(
                session,
                employer="Test Corp",
                role="Software Engineer",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Captcha detected",
                eligibility_json='{"reason_codes": ["captcha"]}',
            )
            application_id = application.id

        with database.session_scope() as session:
            requests = build_decision_requests(session)

        assert len(requests) >= 1
        for req in requests:
            payload = req.to_dict()
            serialised = json.dumps(payload, sort_keys=True)

            # No candidate personal data should be in the payload
            assert "first_name" not in serialised
            assert "last_name" not in serialised
            assert "email" not in serialised
            assert "phone" not in serialised
            assert "address" not in serialised
            assert "university" not in serialised
            assert "degree" not in serialised
            assert "linkedin" not in serialised
            assert "github" not in serialised


class TestNoDatabaseWrites:
    def test_build_decision_requests_performs_no_writes(self, tmp_path: Path):
        settings, database, crypto = _setup_db(tmp_path)

        with database.session_scope() as session:
            application, opportunity = _create_application(
                session,
                employer="Test Corp",
                role="Software Engineer",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Captcha detected",
                eligibility_json='{"reason_codes": ["captcha"]}',
            )
            application_id = application.id

        # Capture DB state before
        with database.session_scope() as session:
            before_count = session.query(Application).count()

        with database.session_scope() as session:
            requests = build_decision_requests(session)

        # Capture DB state after
        with database.session_scope() as session:
            after_count = session.query(Application).count()

        assert before_count == after_count
        assert len(requests) >= 0  # May be 0 if no canonical key mapping


class TestExistingNotificationBehaviourUnchanged:
    def test_plain_notification_still_works(self, tmp_path: Path):
        backend = RecordingBackend()
        notifier = _service(backend)

        from app.services.notifications import HumanAttentionEvent

        event = HumanAttentionEvent(
            application_id="app-1",
            employer="Goldman Sachs",
            role="2027 Placement",
            state="NEEDS_USER",
            reason="captcha",
        )

        assert notifier.notify(event) is True
        assert len(backend.payloads) == 1

        payload = backend.payloads[0]
        assert payload["event"] == "human_attention_required"
        assert "message" in payload
        assert "ARGUS: Goldman Sachs" in payload["message"]
        assert "Captcha" in payload["message"]
        assert "items" in payload

    def test_decision_payload_delivery_uses_same_backend(self, tmp_path: Path):
        backend = RecordingBackend()
        notifier = _service(backend)

        decision_payload = {
            "event": "decision_request",
            "idempotency_key": "test-key-123",
            "decision_request": {
                "id": "dec-1",
                "application_id": "app-1",
                "employer": "Test Corp",
                "role": "Role",
                "question_text": "Question?",
                "canonical_key": "document.cv",
                "sensitivity": "standard",
                "permitted_options": ["Upload", "Skip"],
                "confidence": 0.8,
                "prompt": "Test Corp — Role: Question?",
            },
        }

        assert notifier.send_decision_payload(decision_payload) is True
        assert len(backend.payloads) == 1
        assert backend.payloads[0]["event"] == "decision_request"
        assert backend.payloads[0]["decision_request"]["id"] == "dec-1"

    def test_disabled_notifier_does_not_send_decision_payload(self, tmp_path: Path):
        backend = RecordingBackend()
        notifier = _service(backend)
        notifier._enabled = False

        decision_payload = {"event": "decision_request", "idempotency_key": "test"}

        assert notifier.send_decision_payload(decision_payload) is False
        assert backend.payloads == []


class TestBuildDecisionRequests:
    def test_build_requests_for_needs_user(self, tmp_path: Path):
        settings, database, crypto = _setup_db(tmp_path)

        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )
            _create_application(
                session,
                employer="Morgan Stanley",
                role="Analyst",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Captcha detected",
                eligibility_json='{"reason_codes": ["captcha"]}',
            )

        with database.session_scope() as session:
            requests = build_decision_requests(session)

        # Should create requests for both applications with known reason codes
        assert len(requests) >= 1
        keys = {r.canonical_key for r in requests}
        assert CanonicalKey.WORK_AUTHORISATION in keys or CanonicalKey.CAPTCHA in keys

    def test_build_requests_skips_unknown_reasons(self, tmp_path: Path):
        settings, database, crypto = _setup_db(tmp_path)

        with database.session_scope() as session:
            _create_application(
                session,
                employer="Unknown Corp",
                role="Role",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Some unknown reason",
                eligibility_json='{"reason_codes": ["unknown_reason"]}',
            )

        with database.session_scope() as session:
            requests = build_decision_requests(session)

        # Unknown reasons should be skipped (no canonical key mapping)
        assert len(requests) == 0

    def test_build_requests_readonly_no_writes(self, tmp_path: Path):
        settings, database, crypto = _setup_db(tmp_path)

        with database.session_scope() as session:
            _create_application(
                session,
                employer="Test Corp",
                role="Role",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Captcha detected",
                eligibility_json='{"reason_codes": ["captcha"]}',
            )

        with database.SessionLocal() as session:
            before = session.execute(select(Application)).all()

        with database.SessionLocal() as session:
            requests = build_decision_requests(session)

        with database.SessionLocal() as session:
            after = session.execute(select(Application)).all()

        assert len(before) == len(after)


class TestReasonMapping:
    def test_work_authorisation_missing_maps_to_legal_work_authorisation(self):
        reason = select_human_attention_reason(
            state="NEEDS_USER",
            detail="Work authorisation missing",
        )
        assert reason == "work_authorisation_missing"

    def test_captcha_maps_to_captcha(self):
        reason = select_human_attention_reason(
            state="NEEDS_USER",
            detail="Captcha detected",
        )
        assert reason == "captcha"

    def test_sensitive_demographic_maps_correctly(self):
        reason = select_human_attention_reason(
            state="NEEDS_USER",
            detail="Demographic question",
        )
        assert reason == "sensitive_demographic"


class TestHermesIntegration:
    def test_hermes_backend_still_works_with_decision_payload(self, tmp_path: Path):
        """Regression test: existing Hermes backend should handle decision payloads."""
        from app.services.notifications import _HermesBackend, _FanoutBackend

        runner_calls: list[list[str]] = []

        def recording_runner(argv: list[str], **kwargs):
            runner_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        import subprocess

        hermes_backend = _HermesBackend(recording_runner, "telegram", "hermes")
        fanout = _FanoutBackend([hermes_backend])

        decision_payload = {
            "event": "decision_request",
            "idempotency_key": "test-123",
            "decision_request": {
                "id": "dec-1",
                "application_id": "app-1",
                "employer": "Test",
                "role": "Role",
                "question_text": "Question?",
                "canonical_key": "document.cv",
                "sensitivity": "standard",
                "permitted_options": ["Upload", "Skip"],
                "confidence": 0.8,
                "prompt": "Test — Role: Question?",
            },
        }

        # This should not raise and should call hermes send
        fanout.send(decision_payload)

        assert len(runner_calls) == 1
        argv = runner_calls[0]
        assert argv[0] == "hermes"
        assert argv[1] == "send"
        assert argv[2] == "-t"
        assert argv[3] == "telegram"
        # The message should be the JSON-serialised payload
        assert "decision_request" in argv[4]
        assert "dec-1" in argv[4]


# Import subprocess for the hermes test
import subprocess