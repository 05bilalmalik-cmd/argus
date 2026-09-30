"""Agent07 campaign: bounded decision durability and truthful lifecycle.

Proves, against synthetic loopback fixtures and temp DBs only:
- answer store is scoped to settings.data_dir (two roots isolated despite
  shared LOCALAPPDATA; legacy global file never written);
- cross-process duplicate answers collapse to exactly one winner + one 409;
- corrupt/truncated store fails closed (503, never invented data);
- duplicate POST is 409 and the readback returns the exact written response;
- unresolved decisions stay visible after acknowledgement (no suppression);
- stale-context IDs are rejected for answering but remain readable as
  stale_or_resolved receipts;
- one application's answers are unusable from an unrelated application;
- legal/sensitive tiers reject defaults and stay human-only;
- answering/reading never touches main DB state, arming, or submission;
- auth semantics (404 unset / 401 wrong) hold on all three routes.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.services import decision_store as store
from app.services.decisions import (
    LEGAL_SENSITIVE_KEYS,
    _permitted_options_for_key,
    _question_text_for_key,
    _sensitivity_for_key,
    build_decision_requests,
    decision_context_revision,
    stable_decision_id,
)

TOKEN = "campaign-token"


@pytest.fixture(autouse=True)
def _isolated_localappdata(tmp_path: Path, monkeypatch):
    """Never touch the ambient machine's global answer file.

    Every campaign test gets a private LOCALAPPDATA; cross-sandbox reads
    are covered explicitly by TestDataDirIsolation / TestLegacyCompatibility.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


def _setup_db(data_dir: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(data_dir)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database


def _create_application(
    session,
    *,
    employer: str = "Goldman Sachs",
    role: str = "2027 Placement",
    state: str = ApplicationState.NEEDS_USER.value,
    next_action: str = "Work authorisation missing",
    eligibility_json: str = '{"reason_codes": ["work_authorisation_missing"]}',
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
        conflict_json="{}",
    )
    session.add(application)
    session.flush()
    return application, opportunity


def _app_for(data_dir: Path, monkeypatch, token: str | None = TOKEN):
    # Local import: multiprocessing spawn re-imports this module in the
    # child, and importing app.main at module scope would execute the
    # ambient create_app() there.
    from app.main import create_app

    settings = Settings.load({"ARGUS_DATA_DIR": str(data_dir)})
    settings.ensure_directories()
    if token is None:
        monkeypatch.delenv("ARGUS_DECISION_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ARGUS_DECISION_TOKEN", token)
    return create_app(settings), settings


def _headers(token: str = TOKEN):
    return {"X-Decision-Token": token}


def _seeded_decision_id(data_dir: Path, monkeypatch) -> tuple[str, str]:
    """Seed one NEEDS_USER app; return (decision_id, application_id)."""
    _, database = _setup_db(data_dir)
    with database.session_scope() as session:
        app_row, _ = _create_application(session)
        application_id = app_row.id
    app, _ = _app_for(data_dir, monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/api/decisions", headers=_headers())
        assert resp.status_code == 200
        decisions = resp.json()
        assert len(decisions) >= 1
        return decisions[0]["id"], application_id


def _race_worker(data_dir_str: str, record: dict, queue) -> None:
    """Multiprocessing target: attempt one store write from another process."""
    from app.config import Settings as _Settings
    from app.services import decision_store as _store

    settings = _Settings.load({"ARGUS_DATA_DIR": data_dir_str})
    try:
        queue.put(bool(_store.try_record(settings, dict(record))))
    except Exception as exc:  # noqa: BLE001 - surface worker failure to parent
        queue.put(f"ERROR:{type(exc).__name__}:{exc}")


def _direct_record(decision_id: str, application_id: str) -> dict:
    return {
        "decision_id": decision_id,
        "chosen_option": "Yes",
        "decided_by": "human@phone",
        "decided_at": "2026-09-20T12:00:00+00:00",
        "application_id": application_id,
        "canonical_key": "legal.work_authorisation",
        "sensitivity": "legal",
        "context_revision": "test-revision",
        "question_text": "Are you authorised to work in the UK?",
        "permitted_options": ["Yes", "No"],
        "employer": "Goldman Sachs",
        "role": "2027 Placement",
    }


class TestDataDirIsolation:
    def test_two_roots_isolated_despite_shared_localappdata(
        self, tmp_path: Path, monkeypatch
    ):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        shared_local = tmp_path / "localappdata"
        monkeypatch.setenv("LOCALAPPDATA", str(shared_local))

        decision_id, _ = _seeded_decision_id(root_a, monkeypatch)
        app_a, _ = _app_for(root_a, monkeypatch)
        with TestClient(app_a) as client:
            ans = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            )
            assert ans.status_code == 200

        # New root: answer file lives under root A only.
        assert store.jsonl_path(Settings.load({"ARGUS_DATA_DIR": str(root_a)})).exists()
        assert not store.jsonl_path(
            Settings.load({"ARGUS_DATA_DIR": str(root_b)})
        ).exists()
        # Legacy global sandbox path is never written.
        assert not (shared_local / "ARGUS" / "decisions_answers.jsonl").exists()

        # Root B cannot see or consume root A's decision.
        _setup_db(root_b)
        app_b, _ = _app_for(root_b, monkeypatch)
        with TestClient(app_b) as client:
            assert client.get("/api/decisions", headers=_headers()).json() == []
            resp = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            )
            assert resp.status_code == 404
            assert client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            ).status_code == 404


class TestCrossProcessRace:
    def test_two_processes_one_winner_one_loser(self, tmp_path: Path):
        settings, _ = _setup_db(tmp_path)
        record = _direct_record("race-decision-1", "race-app-1")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        procs = [
            ctx.Process(target=_race_worker, args=(str(tmp_path), record, queue))
            for _ in range(2)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(60)
        assert all(proc.exitcode == 0 for proc in procs)
        outcomes = sorted(str(queue.get()) for _ in range(2))
        assert outcomes == ["False", "True"]
        assert store.read_records(settings)["race-decision-1"]["chosen_option"] == "Yes"


class TestCorruptStoreFailClosed:
    def test_garbage_line_blocks_answer_and_readback(self, tmp_path: Path, monkeypatch):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        path = store.jsonl_path(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not json{{{{\n", encoding="utf-8")
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            )
            assert resp.status_code == 503
            readback = client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            )
            assert readback.status_code == 503
        with pytest.raises(store.DecisionStoreCorruptError):
            store.read_records(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    def test_truncated_line_blocks_readback(self, tmp_path: Path, monkeypatch):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        path = store.jsonl_path(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"decision_id": "truncated', encoding="utf-8")
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            assert (
                client.get(f"/api/decisions/{decision_id}", headers=_headers()).status_code
                == 503
            )

    def test_schema_violation_blocks_readback(self, tmp_path: Path, monkeypatch):
        _seeded_decision_id(tmp_path, monkeypatch)
        path = store.jsonl_path(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"decision_id": "x", "chosen_option": "Yes"}) + "\n",
            encoding="utf-8",
        )
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            listed = client.get("/api/decisions", headers=_headers())
            assert listed.status_code == 200  # listing never invents store data
            assert (
                client.get("/api/decisions/x", headers=_headers()).status_code == 503
            )


class TestDuplicateAndReadback:
    def test_second_answer_is_409_and_readback_matches_exact_bytes(
        self, tmp_path: Path, monkeypatch
    ):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            first = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            )
            assert first.status_code == 200
            written = first.json()
            second = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "No", "decided_by": "human@phone"},
                headers=_headers(),
            )
            assert second.status_code == 409

            readback = client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            )
            assert readback.status_code == 200
            body = readback.json()
            assert body["status"] == "answered"
            assert body["manual_action_needed"] is True
            assert "acknowledgement" in body["note"].casefold()
            assert body["answer"]["chosen_option"] == "Yes"
            assert body["answer"]["decided_by"] == "human@phone"
            assert body["answer"]["decided_at"] == written["decided_at"]
            assert body["request"]["id"] == decision_id

    def test_unresolved_readback_and_no_suppression(self, tmp_path: Path, monkeypatch):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            readback = client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            )
            assert readback.status_code == 200
            body = readback.json()
            assert body["status"] == "unresolved"
            assert body["answer"] is None
            assert body["manual_action_needed"] is True
            assert body["request"]["id"] == decision_id

            # Acknowledging keeps the question visible: no suppression.
            assert client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            ).status_code == 200
            listed = client.get("/api/decisions", headers=_headers()).json()
            assert any(item["id"] == decision_id for item in listed)


class TestStaleContext:
    def test_old_id_rejected_after_context_change_but_receipt_survives(
        self, tmp_path: Path, monkeypatch
    ):
        from app.services import decisions as decisions_module

        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            assert client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            ).status_code == 200

        original = decisions_module._permitted_options_for_key

        def _changed(key):
            if str(key) == "legal.work_authorisation":
                return ("Yes", "No", "Pending review")
            return original(key)

        monkeypatch.setattr(
            decisions_module, "_permitted_options_for_key", _changed
        )
        app2, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app2) as client:
            listed = client.get("/api/decisions", headers=_headers()).json()
            assert listed, "changed context must still list the question"
            assert all(item["id"] != decision_id for item in listed)
            stale_post = client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            )
            assert stale_post.status_code == 404
            assert "stale" in stale_post.json()["detail"].casefold()
            receipt = client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            )
            assert receipt.status_code == 200
            body = receipt.json()
            assert body["status"] == "stale_or_resolved"
            assert body["answer"]["chosen_option"] == "Yes"

    def test_same_context_keeps_deterministic_id(self, tmp_path: Path, monkeypatch):
        _, database = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(session)
        with database.session_scope() as session:
            first = {r.application_id: r for r in build_decision_requests(session)}
        with database.session_scope() as session:
            second = {r.application_id: r for r in build_decision_requests(session)}
        assert set(first) == set(second) and first
        for application_id in first:
            req = first[application_id]
            revision = decision_context_revision(
                canonical_key=req.canonical_key,
                question_text=req.question_text,
                permitted_options=req.permitted_options,
                sensitivity=req.sensitivity,
            )
            assert stable_decision_id(
                application_id, req.canonical_key, context_revision=revision
            ) == stable_decision_id(
                application_id,
                second[application_id].canonical_key,
                context_revision=decision_context_revision(
                    canonical_key=second[application_id].canonical_key,
                    question_text=second[application_id].question_text,
                    permitted_options=second[application_id].permitted_options,
                    sensitivity=second[application_id].sensitivity,
                ),
            )


class TestLegalDefaultsStayHumanOnly:
    def test_default_variants_rejected(self, tmp_path: Path, monkeypatch):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            for variant in ("Default", "default", " DEFAULT "):
                resp = client.post(
                    f"/api/decisions/{decision_id}/answer",
                    json={"chosen_option": variant, "decided_by": "human@phone"},
                    headers=_headers(),
                )
                assert resp.status_code == 400
                assert "explicit choice" in resp.json()["detail"].casefold()

    def test_sensitive_keys_single_sourced(self):
        from app.routers import decisions as router_module
        from app.services import decisions as service_module

        assert router_module.LEGAL_SENSITIVE_KEYS is service_module.LEGAL_SENSITIVE_KEYS
        assert set(LEGAL_SENSITIVE_KEYS) == {
            service_module.CanonicalKey.WORK_AUTHORISATION,
            service_module.CanonicalKey.SPONSORSHIP,
            service_module.CanonicalKey.CRIMINAL_RECORD,
            service_module.CanonicalKey.LEGAL_ATTESTATION,
            service_module.CanonicalKey.DEMOGRAPHIC,
        }


class TestNoMainDbOrArmingEffects:
    def test_answer_and_readback_change_nothing(self, tmp_path: Path, monkeypatch):
        settings, database = _setup_db(tmp_path)
        with database.session_scope() as session:
            app_row, _ = _create_application(session)
            application_id = app_row.id
        with database.session_scope() as session:
            before_state = session.get(Application, application_id).state
            before_apps = session.query(Application).count()
            from app.models import AutomationRun

            before_runs = session.query(AutomationRun).count()

        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            decision_id = client.get("/api/decisions", headers=_headers()).json()[0]["id"]
            assert client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=_headers(),
            ).status_code == 200
            assert client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            ).status_code == 200

        with database.session_scope() as session:
            after = session.get(Application, application_id)
            assert after.state == before_state == ApplicationState.NEEDS_USER.value
            assert after.submission_reference in (None, "")
            assert session.query(Application).count() == before_apps
            from app.models import AutomationRun

            assert session.query(AutomationRun).count() == before_runs
        assert app.state.settings.automation_mode.value in ("OFF", "REVIEW_ONLY")
        assert app.state.settings.submission_armed is False


class TestAuthUnchanged:
    def test_missing_token_404_all_routes(self, tmp_path: Path, monkeypatch):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        app, _ = _app_for(tmp_path, monkeypatch, token=None)
        with TestClient(app) as client:
            assert client.get("/api/decisions").status_code == 404
            assert client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
            ).status_code == 404
            assert client.get(f"/api/decisions/{decision_id}").status_code == 404

    def test_wrong_token_401_all_routes(self, tmp_path: Path, monkeypatch):
        decision_id, _ = _seeded_decision_id(tmp_path, monkeypatch)
        app, _ = _app_for(tmp_path, monkeypatch)
        bad = {"X-Decision-Token": "wrong-token"}
        with TestClient(app) as client:
            assert client.get("/api/decisions", headers=bad).status_code == 401
            assert client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Yes", "decided_by": "human@phone"},
                headers=bad,
            ).status_code == 401
            assert client.get(f"/api/decisions/{decision_id}", headers=bad).status_code == 401


class TestLegacyCompatibility:
    def test_legacy_record_readable_and_preserved(self, tmp_path: Path, monkeypatch):
        legacy_dir = tmp_path / "legacy-local"
        monkeypatch.setenv("LOCALAPPDATA", str(legacy_dir))
        # Compatibility belongs to the old ARGUS data root, not every root.
        tmp_path = legacy_dir / "ARGUS"
        settings, database = _setup_db(tmp_path)
        with database.session_scope() as session:
            app_row, opp = _create_application(session)
            application_id = app_row.id
        with database.session_scope() as session:
            req = build_decision_requests(session)[0]
            revision = decision_context_revision(
                canonical_key=req.canonical_key,
                question_text=req.question_text,
                permitted_options=req.permitted_options,
                sensitivity=req.sensitivity,
            )
            decision_id = stable_decision_id(
                req.application_id, req.canonical_key, context_revision=revision
            )
        legacy_path = store.legacy_jsonl_path()
        assert legacy_path is not None
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_record = {
            "decision_id": decision_id,
            "chosen_option": "No",
            "decided_by": "legacy-human",
            "decided_at": datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
            "application_id": application_id,
            "canonical_key": req.canonical_key.value,
            "sensitivity": req.sensitivity.value,
        }
        with legacy_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(legacy_record, sort_keys=True) + "\n")

        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            assert client.get("/api/decisions", headers=_headers()).status_code == 200
            receipt = client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            )
            assert receipt.status_code == 200
            assert receipt.json()["answer"]["decided_by"] == "legacy-human"
        # Preservation: legacy file untouched, primary store holds no copy.
        assert legacy_path.exists()
        assert json.loads(legacy_path.read_text(encoding="utf-8"))["decided_by"] == (
            "legacy-human"
        )
        assert decision_id in store.read_records(settings)

    def test_action_labels_are_not_field_values(self, tmp_path: Path, monkeypatch):
        """Options like 'Upload CV'/'Completed' are acknowledgement actions.

        They must never be readable as approved document paths or boolean
        field values: the store carries no path, no proof flag, and the
        readback carries an explicit acknowledgement-only note.
        """
        _, database = _setup_db(tmp_path)
        with database.session_scope() as session:
            _create_application(
                session,
                next_action="Captcha detected",
                eligibility_json='{"reason_codes": ["captcha"]}',
            )
        app, _ = _app_for(tmp_path, monkeypatch)
        with TestClient(app) as client:
            decision_id = client.get("/api/decisions", headers=_headers()).json()[0]["id"]
            assert client.post(
                f"/api/decisions/{decision_id}/answer",
                json={"chosen_option": "Completed", "decided_by": "human@phone"},
                headers=_headers(),
            ).status_code == 200
            body = client.get(
                f"/api/decisions/{decision_id}", headers=_headers()
            ).json()
            serialised = json.dumps(body)
            assert "answer_bank" not in serialised
            assert "approved_document" not in serialised
            assert ".pdf" not in serialised.casefold()
            # The note must explicitly DENY that answering equals completion.
            assert "captcha completion" in body["note"].casefold()
            assert "is not field filling" in body["note"].casefold()
            assert "acknowledgement" in body["note"].casefold()
