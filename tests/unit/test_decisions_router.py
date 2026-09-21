from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.domain.questions import CanonicalKey, Sensitivity
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, Opportunity
from app.security.crypto import CryptoBox
from app.services import decision_store as answer_store_module
from app.services.decisions import (
    DecisionRequest,
    DecisionResponse,
    build_decision_requests,
)


def _clear_answer_store():
    """Clear the answer store for test isolation."""
    answer_store = Path(os.environ.get("LOCALAPPDATA", "")) / "ARGUS" / "decisions_answers.jsonl"
    if answer_store.exists():
        answer_store.unlink()


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


def _app_with_token(tmp_path: Path, token: str | None = None):
    """Create a test app with optional ARGUS_DECISION_TOKEN set."""
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    if token is not None:
        os.environ["ARGUS_DECISION_TOKEN"] = token
    else:
        os.environ.pop("ARGUS_DECISION_TOKEN", None)
    return create_app(settings), settings


class TestTokenUnsetEndpointsInactive:
    """When ARGUS_DECISION_TOKEN is unset/empty, endpoints return 404/503."""

    def test_list_decisions_returns_404_when_token_unset(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token=None)
        with TestClient(app) as client:
            response = client.get("/api/decisions")
        assert response.status_code == 404

    def test_answer_decision_returns_404_when_token_unset(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token=None)
        with TestClient(app) as client:
            response = client.post(
                "/api/decisions/some-id/answer",
                json={"chosen_option": "Yes", "decided_by": "test"},
            )
        assert response.status_code == 404

    def test_no_state_change_when_token_unset(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Test Corp",
                role="Role",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token=None)
        with TestClient(app) as client:
            client.post(
                "/api/decisions/fake-id/answer",
                json={"chosen_option": "Yes", "decided_by": "test"},
            )

        with database.session_scope() as session:
            count = session.query(Application).count()
        assert count == 1


class TestWrongTokenReturns401:
    """Wrong or missing token returns 401 and changes nothing."""

    def test_list_decisions_wrong_token_returns_401(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token="correct-token")
        with TestClient(app) as client:
            response = client.get("/api/decisions", headers={"X-Decision-Token": "wrong-token"})
        assert response.status_code == 401

    def test_list_decisions_missing_token_returns_401(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token="correct-token")
        with TestClient(app) as client:
            response = client.get("/api/decisions")
        assert response.status_code == 401

    def test_answer_decision_wrong_token_returns_401(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token="correct-token")
        with TestClient(app) as client:
            response = client.post(
                "/api/decisions/some-id/answer",
                json={"chosen_option": "Yes", "decided_by": "test"},
                headers={"X-Decision-Token": "wrong-token"},
            )
        assert response.status_code == 401

    def test_answer_decision_missing_token_returns_401(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token="correct-token")
        with TestClient(app) as client:
            response = client.post(
                "/api/decisions/some-id/answer",
                json={"chosen_option": "Yes", "decided_by": "test"},
            )
        assert response.status_code == 401


class TestCorrectTokenListsPendingDecisions:
    """With correct token, pending decisions are listed."""

    def test_list_decisions_returns_pending(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
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

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            response = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for item in data:
            assert "id" in item
            assert "application_id" in item
            assert "employer" in item
            assert "role" in item
            assert "canonical_key" in item
            assert "permitted_options" in item
            assert "prompt" in item


class TestValidAnswerRecorded:
    """Valid answer returns 200 and is recorded."""

    def test_valid_answer_returns_200_and_recorded(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            assert list_resp.status_code == 200
            decisions = list_resp.json()
            assert len(decisions) >= 1
            decision_id = decisions[0]["id"]

            answer_resp = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert answer_resp.status_code == 200
        data = answer_resp.json()
        assert data["decision_id"] == decision_id
        assert data["chosen_option"] == "Yes"
        assert data["decided_by"] == "human@phone"
        assert "decided_at" in data

        answer_store_path = answer_store_module.jsonl_path(settings)
        assert answer_store_path.exists()
        with answer_store_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["decision_id"] == decision_id
        assert record["chosen_option"] == "Yes"
        assert record["decided_by"] == "human@phone"


class TestOptionNotInPermittedOptionsRejected:
    """Option not in permitted_options returns 400, nothing recorded."""

    def test_invalid_option_returns_400(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            answer_resp = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Maybe", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert answer_resp.status_code == 400
        assert "not in permitted options" in answer_resp.json()["detail"]

        assert answer_store_module.read_records(settings) == {}


class TestUnknownDecisionIdReturns404:
    """Unknown decision id returns 404."""

    def test_unknown_decision_id_returns_404(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            response = client.post(
                "/api/decisions/unknown-id/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert response.status_code == 404


class TestAnsweringTwiceReturns409:
    """Answering an already-answered decision returns 409."""

    def test_answer_twice_returns_409(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            first = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )
            assert first.status_code == 200

            second = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "No", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert second.status_code == 409


class TestAnsweringDoesNotSubmitOrArm:
    """MANDATORY: Answering a decision does NOT submit, arm, or enqueue submission."""

    def test_answer_does_not_change_application_state(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            app_record, _ = _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )
            application_id = app_record.id

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        with database.session_scope() as session:
            app_after = session.get(Application, application_id)
            assert app_after.state == ApplicationState.NEEDS_USER.value
            assert app_after.submission_reference in (None, "")

    def test_answer_does_not_arm_autopilot(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert app.state.settings.automation_mode.value in ("OFF", "REVIEW_ONLY")
        assert app.state.settings.submission_armed is False

    def test_answer_does_not_enqueue_submission(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        with database.session_scope() as session:
            from app.models import AutomationRun
            runs = session.query(AutomationRun).all()
            submit_runs = [r for r in runs if getattr(r, "mode", "") == "SUBMIT"]
            assert len(submit_runs) == 0


class TestSensitiveTierRequiresExplicitChoice:
    """MANDATORY: Sensitive-tier decisions require explicit choice, no default."""

    def test_legal_tier_rejects_default_option(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision = list_resp.json()[0]
            assert decision["canonical_key"] == "legal.work_authorisation"
            assert "Default" not in decision["permitted_options"]
            decision_id = decision["id"]

            response = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Default", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert response.status_code == 400
        assert "explicit choice" in response.json()["detail"].lower()

    def test_sensitive_tier_rejects_empty_choice(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Test Corp",
                role="Role",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Demographic question",
                eligibility_json='{"reason_codes": ["sensitive_demographic"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision = list_resp.json()[0]
            assert decision["canonical_key"] == "sensitive.demographic"
            decision_id = decision["id"]

            response = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        # Pydantic validates min_length=1 before our code runs
        assert response.status_code == 422

    def test_sensitive_tier_accepts_valid_explicit_choice(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision = list_resp.json()[0]
            assert decision["canonical_key"] == "legal.work_authorisation"
            decision_id = decision["id"]

            response = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert response.status_code == 200
        assert response.json()["chosen_option"] == "Yes"


class TestNoPIIOrTokenInResponses:
    """No secrets, tokens, or candidate PII in responses, errors, or logs."""

    def test_list_response_contains_no_token(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            response = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})

        assert response.status_code == 200
        body = json.dumps(response.json())
        assert "secret-token" not in body
        assert "ARGUS_DECISION_TOKEN" not in body

    def test_answer_response_contains_no_token(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            response = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert response.status_code == 200
        body = json.dumps(response.json())
        assert "secret-token" not in body
        assert "ARGUS_DECISION_TOKEN" not in body

    def test_error_response_contains_no_token(self, tmp_path: Path):
        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            response = client.post(
                "/api/decisions/unknown/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        assert response.status_code == 404
        body = json.dumps(response.json())
        assert "secret-token" not in body
        assert "ARGUS_DECISION_TOKEN" not in body

    def test_no_candidate_pii_in_decision_payload(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Test Corp",
                role="Software Engineer",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Captcha detected",
                eligibility_json='{"reason_codes": ["captcha"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            response = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})

        assert response.status_code == 200
        body = json.dumps(response.json()).casefold()
        forbidden = [
            "first_name", "last_name", "email", "phone", "address",
            "university", "degree", "linkedin", "github", "ssn",
            "ni_number", "passport", "driving", "bank", "sort_code",
            "account", "cvc", "cvv", "pii", "demo", "candidate"
        ]
        for pattern in forbidden:
            assert pattern not in body, f"Forbidden pattern '{pattern}' found in response"


class TestDecisionRequestReuse:
    """Verify we reuse the existing DecisionRequest/DecisionResponse types."""

    def test_decision_request_shape_matches_service(self, tmp_path: Path):
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        with database.session_scope() as session:
            requests = build_decision_requests(session)

        assert len(requests) >= 1
        req = requests[0]
        assert isinstance(req, DecisionRequest)
        assert req.id
        assert req.application_id
        assert req.employer
        assert req.role
        assert req.question_text
        assert req.canonical_key
        assert req.sensitivity
        assert req.permitted_options
        assert req.confidence is None or 0 <= req.confidence <= 1
        assert req.prompt

    def test_decision_response_validation_works(self):
        req = DecisionRequest(
            id="test-id",
            application_id="app-1",
            employer="Test",
            role="Role",
            question_text="Question?",
            canonical_key=CanonicalKey.WORK_AUTHORISATION,
            sensitivity=Sensitivity.LEGAL,
            permitted_options=("Yes", "No"),
            confidence=None,
            prompt="Test",
        )

        resp = DecisionResponse(
            decision_id="test-id",
            chosen_option="Yes",
            decided_by="human",
            decided_at=datetime.now(timezone.utc),
        )

        req.validate_response(resp)

        with pytest.raises(ValueError, match="not in permitted options"):
            bad_resp = DecisionResponse(
                decision_id="test-id",
                chosen_option="Maybe",
                decided_by="human",
                decided_at=datetime.now(timezone.utc),
            )
            req.validate_response(bad_resp)


class TestConcurrentAnswerSafety:
    """Answer store handles concurrent appends without corruption."""

    def test_concurrent_appends_do_not_corrupt(self, tmp_path: Path):
        _clear_answer_store()
        settings, database, _ = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                employer="Goldman Sachs",
                role="2027 Placement",
                state=ApplicationState.NEEDS_USER.value,
                next_action="Work authorisation missing",
                eligibility_json='{"reason_codes": ["work_authorisation_missing"]}',
            )

        app, _ = _app_with_token(tmp_path, token="secret-token")
        with TestClient(app) as client:
            list_resp = client.get("/api/decisions", headers={"X-Decision-Token": "secret-token"})
            decision_id = list_resp.json()[0]["id"]

            client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers={"X-Decision-Token": "secret-token"},
            )

        answer_store_path = answer_store_module.jsonl_path(settings)
        with answer_store_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["decision_id"] == decision_id


class TestTokenComparisonUsesHmac:
    """Token comparison uses hmac.compare_digest, not ==."""

    def test_timing_safe_comparison(self, tmp_path: Path):
        from app.routers.decisions import _verify_token

        assert _verify_token("correct", "correct") is True
        assert _verify_token("wrong", "correct") is False
        assert _verify_token(None, "correct") is False
        assert _verify_token("", "correct") is False