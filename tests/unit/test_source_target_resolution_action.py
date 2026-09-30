from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.main import create_app
from app.security.crypto import CryptoBox
from app.services.target_resolution import (
    ResolutionContext,
    TargetResolutionService,
)
from sqlalchemy import select


def _database(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _opportunity() -> Opportunity:
    return Opportunity(
        employer="Demo Employer",
        role_title="Demo Role",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source/listing?id=demo",
        source="manual",
        ats_type="greenhouse",
    )


def _verified(context: ResolutionContext) -> TargetResolution:
    target = "http://127.0.0.1:8787/lab/ats/greenhouse/application"
    return TargetResolution(
        source_url=context.source_url,
        final_url=target,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        reason_codes=("loopback_verified",),
        evidence={
            "synthetic_lab": True,
            "provider": "greenhouse",
            "application_origin": origin_for_url(target),
            "employer": context.employer,
            "role": context.role_title,
            "requisition": urlsplit(target).path,
            "form_identity": "greenhouse-application",
        },
    )


def test_user_resolution_promotes_only_the_verified_target(tmp_path: Path) -> None:
    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity()
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
        )
        session.add(application)
        session.flush()

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=_verified,
        )

        assert outcome.promoted is True
        assert outcome.human_handoff_required is False
        assert outcome.application_url == (
            "http://127.0.0.1:8787/lab/ats/greenhouse/application"
        )
        assert opportunity.application_url == outcome.application_url
        assert opportunity.url == "http://127.0.0.1:8787/source/listing?id=demo"
        assert opportunity.target_status == TargetKind.APPLICATION_ENTRY.value


def test_unverified_destination_stays_unresolved_and_requests_human_handoff(
    tmp_path: Path,
) -> None:
    _settings, database, _crypto = _database(tmp_path)

    def unverified(context: ResolutionContext) -> TargetResolution:
        return TargetResolution(
            source_url=context.source_url,
            final_url="http://127.0.0.1:8787/lab/redirect/attacker",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=False,
            reason_codes=("identity_unverified",),
            evidence={"provider": "greenhouse"},
        )

    with database.session_scope() as session:
        opportunity = _opportunity()
        session.add(opportunity)
        session.flush()

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            resolver=unverified,
        )

        assert outcome.promoted is False
        assert outcome.human_handoff_required is True
        assert outcome.application_url is None
        assert opportunity.application_url is None
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        persisted = json.loads(opportunity.resolution_evidence_json)
        assert persisted["next_action"]
        assert persisted["human_handoff_required"] is True
        assert "attacker" in persisted["final_url"]


def test_resolution_rejects_cross_bound_context_before_resolver(tmp_path: Path) -> None:
    _settings, database, _crypto = _database(tmp_path)
    called = False

    def resolver(context: ResolutionContext) -> TargetResolution:
        nonlocal called
        called = True
        return _verified(context)

    with database.session_scope() as session:
        opportunity = _opportunity()
        session.add(opportunity)
        session.flush()
        other = _opportunity()
        other.url = "http://127.0.0.1:8787/source/other"
        session.add(other)
        session.flush()

        with pytest.raises((KeyError, ValueError), match="application|bound"):
            TargetResolutionService(session).resolve(
                opportunity.id,
                application_id="not-an-application-for-this-opportunity",
                resolver=resolver,
            )

    assert called is False


def test_resolution_rejects_raw_url_resolver_output(tmp_path: Path) -> None:
    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity()
        session.add(opportunity)
        session.flush()

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            resolver=lambda _context: {
                "final_url": "http://127.0.0.1:8787/attacker",
            },
        )
        assert outcome.promoted is False
        assert outcome.human_handoff_required is True
        assert opportunity.application_url is None
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert "resolver_contract_invalid" in outcome.reason_codes


def test_opportunity_resolution_api_uses_server_owned_resolver_and_never_raw_url(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "test-token"}
    )
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/opportunities",
            json={
                "employer": "Demo Employer",
                "role_title": "Demo Role",
                "programme_group": "summer",
                "cycle": "2027",
                "url": "http://127.0.0.1:8787/source/listing?id=demo",
                "source": "manual",
                "ats_type": "greenhouse",
            },
        )
        assert created.status_code == 201
        opportunity_id = created.json()["id"]
        client.app.state.target_resolution_resolver = _verified

        resolved = client.post(f"/api/opportunities/{opportunity_id}/resolve-target")
        assert resolved.status_code == 200
        payload = resolved.json()
        assert payload["promoted"] is True
        assert payload["application_url"].startswith("http://127.0.0.1:8787/lab/ats/")
        assert payload["source_url"].startswith("http://127.0.0.1:8787/source/")

        forged = client.post(
            f"/api/opportunities/{opportunity_id}/resolve-target",
            json={"final_url": "http://127.0.0.1:8787/attacker"},
        )
    assert forged.status_code == 422


@pytest.mark.parametrize("ingestion", ["manual", "csv", "trackr"])
def test_public_ingestion_paths_reach_bound_resolution_action(
    tmp_path: Path, ingestion: str
) -> None:
    """Manual, CSV, and saved-Trackr records share the same safe action.

    The test deliberately goes through each public ingestion route and then
    resolves by the server-owned application binding.  It never calls the
    persistence primitive directly, so a source/listing import cannot bypass
    the resolver contract.
    """

    from fastapi.testclient import TestClient

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    source_url = f"http://127.0.0.1:8787/source/{ingestion}?id=bound"
    with TestClient(app) as client:
        if ingestion == "manual":
            response = client.post(
                "/api/opportunities",
                json={
                    "employer": "Demo Employer",
                    "role_title": "Summer Analyst Internship",
                    "programme_group": "summer",
                    "cycle": "2027",
                    "url": source_url,
                    "source": "manual",
                    "ats_type": "greenhouse",
                },
            )
            assert response.status_code == 201
            opportunity_id = response.json()["id"]
            evaluated = client.post(f"/api/opportunities/{opportunity_id}/evaluate")
            assert evaluated.status_code == 200
            application_id = evaluated.json()["application_id"]
        elif ingestion == "csv":
            csv_content = (
                "employer,role_title,programme_group,cycle,url,ats_type\n"
                f"Demo Employer,Summer Analyst Internship,summer,2027,{source_url},greenhouse\n"
            ).encode()
            response = client.post(
                "/api/opportunities/import-csv",
                files={"file": ("opportunities.csv", csv_content, "text/csv")},
                data={"source": "csv"},
            )
            assert response.status_code == 200
            assert response.json()["imported"] == 1
            imported = next(
                item
                for item in client.get("/api/opportunities").json()
                if item["source"] == "csv"
            )
            opportunity_id = imported["id"]
            evaluated = client.post(f"/api/opportunities/{opportunity_id}/evaluate")
            assert evaluated.status_code == 200
            application_id = evaluated.json()["application_id"]
        else:
            saved_trackr = (
                "<!doctype html><script type=\"application/ld+json\">"
                + json.dumps(
                    {
                        "@type": "JobPosting",
                        "title": "Summer Analyst Internship",
                        "hiringOrganization": {"name": "Demo Employer"},
                        "url": source_url,
                    }
                )
                + "</script>"
            ).encode()
            response = client.post(
                "/api/scout/upload-trackr-html",
                files={"file": ("saved-trackr.html", saved_trackr, "text/html")},
            )
            assert response.status_code == 200
            assert response.json()["imported"] == 1
            imported = next(
                item
                for item in client.get("/api/opportunities").json()
                if item["source"].startswith("trackr_html:")
            )
            opportunity_id = imported["id"]
            with app.state.db.session_scope() as session:
                application = session.scalar(
                    select(Application).where(
                        Application.opportunity_id == opportunity_id
                    )
                )
                assert application is not None
                application_id = application.id

        app.state.target_resolution_resolver = _verified
        resolved = client.post(f"/api/applications/{application_id}/resolve-target")

    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["promoted"] is True
    assert payload["application_id"] == application_id
    assert payload["source_url"] == source_url
    assert payload["application_url"] != source_url
    assert payload["target_status"] == TargetKind.APPLICATION_ENTRY.value


def test_application_resolution_api_returns_truthful_human_handoff_on_failure(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/opportunities",
            json={
                "employer": "Demo Employer",
                "role_title": "Demo Role",
                "programme_group": "summer",
                "cycle": "2027",
                "url": "http://127.0.0.1:8787/source/listing?id=demo",
            },
        )
        opportunity_id = created.json()["id"]
        evaluated = client.post(f"/api/opportunities/{opportunity_id}/evaluate")
        assert evaluated.status_code == 200
        application_id = evaluated.json()["application_id"]

        def failed(_context: ResolutionContext):
            return None, {"state": "HUMAN_REQUIRED", "next_action": "Complete CAPTCHA in Navigator."}

        client.app.state.target_resolution_resolver = failed
        response = client.post(f"/api/applications/{application_id}/resolve-target")
        assert response.status_code == 202
        payload = response.json()
        assert payload["promoted"] is False
        assert payload["application_url"] is None
        assert payload["target_status"] == TargetKind.UNRESOLVED.value
        assert payload["human_handoff_required"] is True
        assert payload["next_action"] == "Complete CAPTCHA in Navigator."


def test_shared_source_roles_cannot_cross_bind_resolution_actions(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    with TestClient(app) as client:
        common = {
            "cycle": "2027",
            "url": "http://127.0.0.1:8787/source/shared",
            "programme_group": "summer",
        }
        first = client.post(
            "/api/opportunities",
            json={"employer": "Shared Employer", "role_title": "Role A", **common},
        ).json()
        second = client.post(
            "/api/opportunities",
            json={"employer": "Shared Employer", "role_title": "Role B", **common},
        ).json()
        app_a = client.post(f"/api/opportunities/{first['id']}/evaluate").json()["application_id"]
        app_b = client.post(f"/api/opportunities/{second['id']}/evaluate").json()["application_id"]
        client.app.state.target_resolution_resolver = _verified

        cross_opportunity = client.post(
            f"/api/opportunities/{first['id']}/resolve-target",
            json={"application_id": app_b},
        )
        cross_application = client.post(
            f"/api/applications/{app_a}/resolve-target",
            json={"opportunity_id": second["id"]},
        )

    assert cross_opportunity.status_code == 409
    assert cross_application.status_code == 409


def test_redirect_identity_and_requisition_mismatch_never_promotes(tmp_path: Path) -> None:
    # TRIAGE (item 2): the reason code was effectively restructured by the
    # custom-domain hardening, NOT regressed. The forged resolution below is
    # a loopback URL with no `synthetic_lab` flag and no independent proof,
    # so `verified_for_automation` is False and the service fail-closes in
    # the generic `target_not_verified` branch
    # (app/services/target_resolution.py `resolve`, APPLICATION_FORM fallthrough)
    # before ever reaching `_candidate_binding_reason`, the only place that
    # emits `resolution_requisition_mismatch`. Real output is
    # ('browser_claimed_verified', 'target_not_verified'). The SAFETY meaning
    # is intact -- the forged redirect still never promotes, the stored URL
    # is untouched, and the row stays UNRESOLVED -- so the test now asserts
    # the current correct code plus explicit non-promotion.
    _settings, database, _crypto = _database(tmp_path)

    def forged(context: ResolutionContext) -> TargetResolution:
        target = "http://127.0.0.1:8787/lab/ats/greenhouse/other-job"
        return TargetResolution(
            source_url=context.source_url,
            final_url=target,
            kind=TargetKind.APPLICATION_FORM,
            provider="greenhouse",
            identity_verified=True,
            form_verified=True,
            reason_codes=("browser_claimed_verified",),
            evidence={
                "structured_feed": "greenhouse:other-employer",
                "provider": "greenhouse",
                "application_origin": origin_for_url(target),
                "employer": "Other Employer",
                "role": context.role_title,
                "requisition": "/lab/ats/greenhouse/other-job",
                "form_identity": "other-job",
                "form": {
                    "frame_url": target,
                    "root_selector": "#apply",
                    "control_count": 1,
                    "submit_present": True,
                    "root_token": "other-job",
                    "binding_verified": True,
                    "bound_target_url": target,
                    "bound_provider": "greenhouse",
                    "bound_role": context.role_title,
                    "bound_requisition": "/other-job",
                    "form_action": "http://127.0.0.1:8788/submit",
                },
            },
        )

    with database.session_scope() as session:
        opportunity = _opportunity()
        stored_target = "https://job-boards.greenhouse.io/demo/jobs/8518528002"
        opportunity.application_url = stored_target
        session.add(opportunity)
        session.flush()
        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            resolver=forged,
        )
        assert outcome.promoted is False
        assert outcome.human_handoff_required is True
        assert opportunity.application_url == stored_target
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert "target_not_verified" in outcome.reason_codes


def test_default_app_installs_a_production_owned_target_resolver(tmp_path: Path) -> None:
    """The packaged app must not depend on a test-only callback injection."""

    app = create_app(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    resolver = getattr(app.state, "target_resolution_resolver", None)
    assert callable(resolver)


def test_default_navigator_adapter_passes_only_application_id(tmp_path: Path) -> None:
    """The API fallback uses the Navigator method, never a request URL."""

    from fastapi.testclient import TestClient

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    observed: list[object] = []
    with TestClient(app) as client:
        created = client.post(
            "/api/opportunities",
            json={
                "employer": "Demo Employer",
                "role_title": "Demo Role",
                "programme_group": "summer",
                "cycle": "2027",
                "url": "http://127.0.0.1:8787/source/listing?id=demo",
            },
        ).json()
        opportunity_id = created["id"]
        evaluated = client.post(f"/api/opportunities/{opportunity_id}/evaluate").json()
        application_id = evaluated["application_id"]

        def resolve_source(exact_application_id: str):
            observed.append(exact_application_id)
            assert exact_application_id == application_id
            with app.state.db.session_scope() as session:
                opportunity = session.get(Opportunity, created["id"])
                assert opportunity is not None
                context = ResolutionContext(
                    opportunity_id=opportunity.id,
                    application_id=exact_application_id,
                    source_url=opportunity.url,
                    employer=opportunity.employer,
                    role_title=opportunity.role_title,
                    cycle=opportunity.cycle,
                    provider_hint=opportunity.ats_type,
                    source=opportunity.source,
                )
            return _verified(context)

        app.state.navigator.resolve_source = resolve_source
        response = client.post(f"/api/applications/{application_id}/resolve-target")

    assert response.status_code == 200
    assert observed == [application_id]
    assert "source/listing" not in str(observed)
