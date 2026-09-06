from pathlib import Path
from types import SimpleNamespace
from typing import get_args

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.routers.deps import SessionDep
from tests.document_helpers import cv_docx_bytes


def client(tmp_path: Path) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "test-token"})
    return TestClient(create_app(settings))


def test_database_dependency_commits_before_http_response_is_sent() -> None:
    dependency = next(
        metadata
        for metadata in get_args(SessionDep)[1:]
        if hasattr(metadata, "scope")
    )
    assert dependency.scope == "function"


def test_profile_answers_documents_and_opportunity_pipeline_api(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        profile = api.put(
            "/api/profile",
            json={
                "first_name": "Demo",
                "last_name": "Candidate",
                "email": "demo@example.test",
                "university": "Example University",
                "degree": "BSc Finance",
                "graduation_year": 2029,
                "work_authorisation": "Approved test wording",
                "requires_sponsorship": False,
                "work_authorisation_approved": True,
            },
        )
        assert profile.status_code == 200
        assert profile.json()["first_name"] == "Demo"
        assert "work_authorisation" not in profile.json()

        answer = api.post(
            "/api/answers",
            json={
                "canonical_key": "answer.motivation",
                "prompt": "Why this role?",
                "answer": "Evidence-backed motivation.",
                "approved": True,
                "sensitive": False,
            },
        )
        assert answer.status_code == 201
        assert answer.json()["answer_preview"].startswith("Evidence-backed")

        document = api.post(
            "/api/documents",
            data={
                "kind": "cv",
                "approved": "true",
                "tags": "investment-banking,london,summer-cv",
            },
            files={
                "file": (
                    "Synthetic API CV.docx",
                    cv_docx_bytes(2028, "API pipeline"),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert document.status_code == 201
        assert document.json()["name"] == "Synthetic_API_CV.docx"
        assert document.json()["sha256"]

        opportunity = api.post(
            "/api/opportunities",
            json={
                "employer": "ARGUS Test Capital",
                "role_title": "Summer Analyst",
                "division": "Investment Banking",
                "programme_group": "summer",
                "location": "London",
                "cycle": "2027",
                "url": "http://127.0.0.1:8787/lab/ats/standard",
                "source": "manual",
                "ats_type": "greenhouse",
                "sponsorship_supported": True,
                "cv_required": True,
            },
        )
        assert opportunity.status_code == 201
        opportunity_id = opportunity.json()["id"]

        evaluated = api.post(f"/api/opportunities/{opportunity_id}/evaluate")
        assert evaluated.status_code == 200
        application_id = evaluated.json()["application_id"]
        assert evaluated.json()["eligible"] is True

        assert api.post(f"/api/applications/{application_id}/queue").status_code == 200
        prepared = api.post(f"/api/applications/{application_id}/prepare")
        assert prepared.status_code == 200
        assert prepared.json()["ready"] is True

        applications = api.get("/api/applications")
        assert applications.status_code == 200
        assert applications.json()[0]["state"] == "PACKAGE_PREPARED"


def test_capture_endpoint_requires_exact_local_token_and_deduplicates(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        payload = {
            "employer": "Captured Firm",
            "role_title": "Analyst Intern",
            "cycle": "2027",
            "url": "https://jobs.example.test/role?utm_source=trackr",
        }
        assert api.post("/api/capture", json=payload).status_code == 401
        first = api.post("/api/capture", json=payload, headers={"X-Argus-Token": "test-token"})
        second = api.post("/api/capture", json=payload, headers={"X-Argus-Token": "test-token"})

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]


def test_create_endpoint_uses_exact_identity_under_forced_fingerprint_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A digest collision must not reject a different location/division."""

    def forced_fingerprint(**_kwargs):
        return "forced-api-collision"

    monkeypatch.setattr(
        "app.services.opportunities.source_fingerprint",
        forced_fingerprint,
    )
    shared = {
        "employer": "Collision Capital",
        "role_title": "Summer Analyst",
        "cycle": "2027",
        "url": "https://careers.example.test/shared-programme",
    }
    london_credit = {
        **shared,
        "location": "London",
        "division": "Private Credit",
    }
    paris_equity = {
        **shared,
        "location": "Paris",
        "division": "Private Equity",
    }

    with client(tmp_path) as api:
        first = api.post("/api/opportunities", json=london_credit)
        distinct = api.post("/api/opportunities", json=paris_equity)
        duplicate = api.post("/api/opportunities", json=london_credit)

    assert first.status_code == 201
    assert distinct.status_code == 201
    assert distinct.json()["id"] != first.json()["id"]
    assert duplicate.status_code == 409


def test_capture_endpoint_returns_only_exact_duplicate_under_forced_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Capture returns 200 only for the same full source identity."""

    def forced_fingerprint(**_kwargs):
        return "forced-capture-collision"

    monkeypatch.setattr(
        "app.services.opportunities.source_fingerprint",
        forced_fingerprint,
    )
    headers = {"X-Argus-Token": "test-token"}
    shared = {
        "employer": "Captured Collision Firm",
        "role_title": "Analyst Intern",
        "cycle": "2027",
        "url": "https://jobs.example.test/shared-role",
    }
    london = {**shared, "location": "London", "division": "Credit"}
    paris = {**shared, "location": "Paris", "division": "Equity"}

    with client(tmp_path) as api:
        first = api.post("/api/capture", json=london, headers=headers)
        distinct = api.post("/api/capture", json=paris, headers=headers)
        duplicate = api.post("/api/capture", json=london, headers=headers)

    assert first.status_code == 201
    assert distinct.status_code == 201
    assert distinct.json()["id"] != first.json()["id"]
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == first.json()["id"]


def test_dashboard_and_page_routes_are_available(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        dashboard = api.get("/api/dashboard")
        page = api.get("/")
        lab = api.get("/lab")

        assert dashboard.status_code == 200
        assert dashboard.json()["total_opportunities"] == 0
        assert page.status_code == 200
        assert "ARGUS" in page.text
        assert "Command Centre" in page.text
        assert lab.status_code == 200
        assert "ATS Laboratory" in lab.text


def test_submit_api_requires_explicit_action_time_confirmation(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        schema = api.get("/openapi.json").json()
        parameters = schema["paths"]["/api/applications/{application_id}/run"]["post"].get(
            "parameters", []
        )
        assert "confirm_application_id" not in {item["name"] for item in parameters}
        missing = api.post(
            "/api/applications/example-application/run",
            params={"mode": "submit"},
        )
        incomplete = api.post(
            "/api/applications/example-application/run",
            params={"mode": "submit"},
            json={"application_id": "different-application"},
        )
        mismatched = api.post(
            "/api/applications/example-application/run",
            params={"mode": "submit"},
            json={
                "application_id": "different-application",
                "session_id": "review-session",
                "authority_id": "submission-authority",
            },
        )

    for response in (missing, mismatched):
        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Action-time JSON confirmation is required for this exact application before submit"
        )
    assert incomplete.status_code == 422
    missing_fields = {tuple(item["loc"]) for item in incomplete.json()["detail"]}
    assert missing_fields == {("body", "session_id"), ("body", "authority_id")}


def test_submit_api_preserves_exact_confirmed_single_application_path(
    tmp_path: Path, monkeypatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(
        runner,
        application_id,
        mode,
        *,
        headed=False,
        session_id="",
        authority_id="",
    ):  # noqa: ANN001
        observed.update(
            application_id=application_id,
            mode=mode.value,
            headed=headed,
            session_id=session_id,
            authority_id=authority_id,
        )
        return SimpleNamespace(
            run_id="confirmed-run",
            state="CONFIRMATION_VERIFIED",
            risk_level=0,
            adapter="greenhouse",
            blocked_reasons=(),
            receipt=None,
            trace_path="",
            screenshot_path="",
            handoff_session_id="",
            human_boundary=None,
        )

    monkeypatch.setattr("app.routers.api.AutomationRunner.run", fake_run)
    with client(tmp_path) as api:
        response = api.post(
            "/api/applications/exact-application/run",
            params={
                "mode": "submit",
                "headed": "true",
            },
            json={
                "application_id": "exact-application",
                "session_id": "review-session",
                "authority_id": "submission-authority",
            },
        )

    assert response.status_code == 200
    assert observed == {
        "application_id": "exact-application",
        "mode": "submit",
        "headed": True,
        "session_id": "review-session",
        "authority_id": "submission-authority",
    }


def test_capture_endpoint_rejects_non_http_urls(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        response = api.post(
            "/api/capture",
            headers={"X-Argus-Token": "test-token"},
            json={
                "employer": "Hostile Capture",
                "role_title": "Analyst",
                "cycle": "2027",
                "url": "javascript://attacker.example/payload",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Application URL must use http or https"


def test_conflict_rules_can_be_created_listed_and_deleted(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        created = api.post(
            "/api/conflict-rules",
            json={
                "employer_pattern": "Example Bank*",
                "cycle": "2027",
                "max_applications": 1,
                "exclusive_groups": "ibd,markets,asset_management; credit,real_assets",
                "notes": "Public recruitment rule",
            },
        )
        listed = api.get("/api/conflict-rules")
        settings_page = api.get("/settings")
        deleted = api.delete(f"/api/conflict-rules/{created.json()['id']}")
        empty = api.get("/api/conflict-rules")

    assert created.status_code == 201
    assert created.json()["exclusive_groups"] == [
        ["asset_management", "ibd", "markets"],
        ["credit", "real_assets"],
    ]
    assert listed.json()[0]["employer_pattern"] == "Example Bank*"
    assert "Employer application rules" in settings_page.text
    assert "Example Bank*" in settings_page.text
    assert deleted.status_code == 204
    assert empty.json() == []
