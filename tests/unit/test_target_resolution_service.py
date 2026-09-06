from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.routers.api import OpportunityPayload, _opportunity, create_opportunity
from app.scouting.programmes import ProgrammeType
from app.scouting.service import ScoutService
from app.scouting.trackr import ScrapedOpportunity
from app.security.crypto import CryptoBox


def _database(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _unresolved_opportunity() -> Opportunity:
    return Opportunity(
        employer="Acme Capital",
        role_title="Summer Analyst",
        programme_group=ProgrammeType.SUMMER.value,
        cycle="2027",
        url="https://aggregator.example.test/jobs/acme-summer",
        source="aggregator",
    )


def test_record_does_not_promote_unverified_target(tmp_path: Path) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/1234",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=False,
            reason_codes=("direct_ats_job", "identity_unverified"),
            evidence={"redirect_count": 1},
        )

        recorded = TargetResolutionService(session).record(opportunity.id, result)

        assert recorded.application_url is None
        assert recorded.automation_url is None
        assert recorded.target_status == TargetKind.APPLICATION_ENTRY.value
        assert recorded.resolution_attempted_at is not None
        assert recorded.resolved_at is None
        assert json.loads(recorded.resolution_evidence_json)["reason_codes"] == [
            "direct_ats_job",
            "identity_unverified",
        ]


def test_apply_hop_diagnosis_survives_resolution_evidence_persistence(
    tmp_path: Path,
) -> None:
    from app.services.navigator import _SourceResolutionExecutor
    from app.services.target_resolution import TargetResolutionService

    class _Page:
        url = "https://aggregator.example.test/jobs/acme-summer"

        @staticmethod
        def content() -> str:
            return "<main><h1>Summer Analyst</h1><p>Acme Capital</p></main>"

        @staticmethod
        def evaluate(script, *_args):
            if "const candidates" in script:
                return {
                    "href": "",
                    "count": 0,
                    "control_count": 0,
                    "visible_root_count": 2,
                    "bound_job_root_found": False,
                    "page_apply_affordance_count": 1,
                    "bound_apply_affordance_count": 0,
                    "candidate_urls": [],
                }
            return {}

        @staticmethod
        def goto(*_args, **_kwargs) -> None:
            pytest.fail("an unbound Apply control must not be followed")

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        session.add(opportunity)
        session.flush()
        executor = _SourceResolutionExecutor(
            source_url=opportunity.url,
            provider_hint="unknown",
            employer=opportunity.employer,
            role_title=opportunity.role_title,
        )
        executor.prepare(_Page())
        assert executor.resolution is not None

        recorded = TargetResolutionService(session).record(
            opportunity.id,
            executor.resolution,
        )

        persisted = json.loads(recorded.resolution_evidence_json)
        assert "apply_bound_job_root_not_found" in persisted["reason_codes"]
        hop = persisted["evidence"]["apply_hop"]
        assert hop["visible_root_count"] == 2
        assert hop["bound_job_root_found"] is False
        assert hop["page_apply_affordance_count"] == 1
        assert hop["eligible_destination_count"] == 0
        assert hop["egress_guard"] == "not_reached"


def test_record_promotes_only_identity_verified_application_entry(tmp_path: Path) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.application_url = (
            "https://boards.greenhouse.io/acme/jobs/1234?gh_src=feed"
        )
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/1234?gh_src=feed",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            reason_codes=("structured_provider_feed",),
            evidence={
                "structured_feed": "greenhouse:acme",
                "requisition": "1234",
            },
        )

        recorded = TargetResolutionService(session).record(opportunity.id, result)

        assert recorded.application_url == (
            "https://boards.greenhouse.io/acme/jobs/1234?gh_src=feed"
        )
        assert recorded.automation_url == recorded.application_url
        assert recorded.target_status == TargetKind.APPLICATION_ENTRY.value
        assert recorded.resolved_ats_type == "greenhouse"
        assert recorded.resolved_at is not None


def test_nested_urls_and_free_form_tokens_are_scrubbed_from_persisted_evidence(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/1234",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=False,
            evidence={
                    "nested": {
                        "url": (
                            "https://acme.test/apply?token=SECRET-TOKEN&email=alex@example.com"
                        "&signature=SIGNATURE-SECRET&state=STATE-SECRET&code=OAUTH-CODE"
                        "&client_secret=CLIENT-SECRET&auth=AUTH-SECRET&ok=1"
                        ),
                    "note": (
                        "Bearer secret-bearer-value api_token=plain-secret "
                        "signature=INLINE-SIGNATURE state=INLINE-STATE "
                        "code=INLINE-CODE client_secret=INLINE-CLIENT "
                        "auth=INLINE-AUTH and alex@example.com"
                    ),
                    "json_text": [
                        '{"auth":"JSON-AUTH","url":"https://acme.test/apply?auth=URL-AUTH&ok=1"}'
                    ],
                    }
            },
        )

        TargetResolutionService(session).record(opportunity.id, result)

        persisted = opportunity.resolution_evidence_json
        assert "SECRET-TOKEN" not in persisted
        assert "secret-bearer-value" not in persisted
        assert "plain-secret" not in persisted
        assert "alex@example.com" not in persisted
        for secret in (
            "SIGNATURE-SECRET",
            "STATE-SECRET",
            "OAUTH-CODE",
            "CLIENT-SECRET",
            "AUTH-SECRET",
            "INLINE-SIGNATURE",
            "INLINE-STATE",
            "INLINE-CODE",
            "INLINE-CLIENT",
            "INLINE-AUTH",
            "JSON-AUTH",
            "URL-AUTH",
        ):
            assert secret not in persisted
        assert "ok=1" in persisted


def test_evidence_keys_are_scrubbed_when_they_contain_free_form_pii(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/1234",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            evidence={"contact_alex@example.com": {"token": "SECRET-TOKEN"}},
        )

        TargetResolutionService(session).record(opportunity.id, result)

        persisted = opportunity.resolution_evidence_json
        assert "alex@example.com" not in persisted
        assert "SECRET-TOKEN" not in persisted


def test_mismatch_attempt_preserves_previous_verified_target(tmp_path: Path) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.application_url = "https://boards.greenhouse.io/acme/jobs/1234"
        session.add(opportunity)
        session.flush()
        service = TargetResolutionService(session)
        verified_url = "https://boards.greenhouse.io/acme/jobs/1234"
        service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url=verified_url,
                kind=TargetKind.APPLICATION_ENTRY,
                provider="greenhouse",
                identity_verified=True,
                evidence={
                    "structured_feed": "greenhouse:acme",
                    "requisition": "1234",
                },
            ),
        )
        prior_status = opportunity.target_status
        prior_resolved_at = opportunity.resolved_at

        recorded = service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url="https://jobs.lever.co.attacker.test/acme/other",
                kind=TargetKind.MISMATCH,
                provider="",
                reason_codes=("provider_mismatch",),
                evidence={"nested": {"token": "MISMATCH-SECRET"}},
            ),
        )

        assert recorded.application_url == verified_url
        assert recorded.target_status == TargetKind.MISMATCH.value
        assert recorded.automation_url is None
        assert recorded.resolved_at == prior_resolved_at
        evidence = json.loads(recorded.resolution_evidence_json)
        assert evidence["kind"] == TargetKind.MISMATCH.value
        assert evidence["promoted"] is False
        assert evidence["preserved_verified_target"] is True
        assert evidence["previous_target_status"] == prior_status
        assert evidence["verified_target"]["target_status"] == prior_status
        assert "MISMATCH-SECRET" not in recorded.resolution_evidence_json

        second = service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url="https://attacker.example.test/acme/other",
                kind=TargetKind.UNRESOLVED,
                provider="",
                reason_codes=("hostile_redirect",),
            ),
        )

        assert second.application_url == verified_url
        assert second.target_status == TargetKind.UNRESOLVED.value
        assert second.automation_url is None
        assert second.resolved_ats_type == "greenhouse"
        assert second.resolved_at == prior_resolved_at
        second_evidence = json.loads(second.resolution_evidence_json)
        assert second_evidence["kind"] == TargetKind.UNRESOLVED.value
        assert second_evidence["attempt_kind"] == TargetKind.UNRESOLVED.value
        assert second_evidence["preserved_verified_target"] is True
        assert second_evidence["verified_target"]["target_status"] == prior_status


def test_repeated_failed_resolution_is_excluded_from_autopilot_until_reverified(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    settings, database, crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.application_url = "https://boards.greenhouse.io/acme/jobs/1234"
        session.add(opportunity)
        session.flush()
        session.add(
            Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.DISCOVERED.value,
            )
        )
        session.flush()
        service = TargetResolutionService(session)
        verified_url = "https://boards.greenhouse.io/acme/jobs/1234"
        service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url=verified_url,
                kind=TargetKind.APPLICATION_ENTRY,
                provider="greenhouse",
                identity_verified=True,
                    evidence={
                        "structured_feed": "greenhouse:acme",
                        "requisition": "1234",
                    },
            ),
        )
        assert opportunity.automation_url == verified_url
        assert ScoutService(session, settings, crypto).autopilot_candidates()

        service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url="https://attacker.example.test/acme/other",
                kind=TargetKind.MISMATCH,
                provider="",
                reason_codes=("hostile_redirect",),
            ),
        )
        assert opportunity.application_url == verified_url
        assert opportunity.target_status == TargetKind.MISMATCH.value
        assert opportunity.automation_url is None
        assert ScoutService(session, settings, crypto).autopilot_candidates() == []

        service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url="https://attacker.example.test/acme/again",
                kind=TargetKind.UNRESOLVED,
                provider="",
                reason_codes=("unresolved",),
            ),
        )
        assert opportunity.application_url == verified_url
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert opportunity.automation_url is None
        assert ScoutService(session, settings, crypto).autopilot_candidates() == []


def test_closed_target_invalidates_ready_state_and_stale_verified_url(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import ResolutionContext, TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.application_url = "https://boards.greenhouse.io/acme/jobs/1234"
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.READY_TO_SUBMIT.value,
            next_action="Review and submit",
        )
        session.add(application)
        session.flush()
        service = TargetResolutionService(session)
        service.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url="https://boards.greenhouse.io/acme/jobs/1234",
                kind=TargetKind.APPLICATION_ENTRY,
                provider="greenhouse",
                identity_verified=True,
                evidence={
                    "structured_feed": "greenhouse:acme",
                    "requisition": "1234",
                },
            ),
        )

        def closed(context: ResolutionContext) -> TargetResolution:
            return TargetResolution(
                source_url=context.source_url,
                final_url=context.source_url,
                kind=TargetKind.BLOCKED,
                provider="",
                reason_codes=("job_closed_or_missing",),
            )

        outcome = service.resolve(
            opportunity.id,
            application_id=application.id,
            resolver=closed,
        )

        assert outcome.promoted is False
        assert outcome.target_status == TargetKind.BLOCKED.value
        assert opportunity.application_url is None
        assert opportunity.automation_url is None
        assert application.state == ApplicationState.BLOCKED.value
        assert application.next_action == "Find a current application target"


def test_unresolved_recheck_moves_stranded_ready_application_to_needs_user(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.READY_TO_SUBMIT.value,
            next_action="Review and submit",
        )
        session.add(application)
        session.flush()

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=lambda _context: None,
        )

        assert outcome.promoted is False
        assert application.state == ApplicationState.NEEDS_USER.value
        assert application.next_action == outcome.next_action


def test_api_serializes_source_and_verified_application_separately(tmp_path: Path) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        lever_url = (
            "https://jobs.lever.co/acme/"
            "9f73569e-952b-4c0a-b77c-d302846f15bd/apply"
        )
        opportunity.application_url = lever_url
        session.add(opportunity)
        session.flush()
        TargetResolutionService(session).record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.url,
                final_url=lever_url,
                kind=TargetKind.APPLICATION_ENTRY,
                provider="lever",
                identity_verified=True,
                reason_codes=("structured_provider_feed",),
                evidence={
                    "structured_feed": "lever:acme",
                    "requisition": "9f73569e-952b-4c0a-b77c-d302846f15bd",
                },
            ),
        )

        payload = _opportunity(opportunity)

        assert payload["url"] == "https://aggregator.example.test/jobs/acme-summer"
        assert payload["source_url"] == payload["url"]
        assert payload["navigation_url"] == payload["url"]
        assert payload["application_url"] == lever_url
        assert payload["automation_url"] == payload["application_url"]
        assert payload["target_status"] == "APPLICATION_ENTRY"
        assert payload["resolved_ats_type"] == "lever"


def test_api_allows_distinct_roles_to_share_one_source_page(tmp_path: Path) -> None:
    _settings, database, _crypto = _database(tmp_path)
    shared_source = "https://careers.example.test/students?utm_source=mail"
    with database.session_scope() as session:
        first = create_opportunity(
            OpportunityPayload(
                employer="Acme Capital",
                role_title="Summer Analyst",
                cycle="2027",
                url=shared_source,
            ),
            session,
        )
        second = create_opportunity(
            OpportunityPayload(
                employer="Acme Capital",
                role_title="Technology Internship",
                cycle="2027",
                url=shared_source,
            ),
            session,
        )

        assert first["id"] != second["id"]
        assert session.query(Opportunity).count() == 2
        assert {item.url for item in session.query(Opportunity).all()} == {
            shared_source
        }


def test_persisted_source_url_is_navigation_target_not_canonical_key(
    tmp_path: Path,
) -> None:
    from app.services.opportunities import OpportunityService

    _settings, database, _crypto = _database(tmp_path)
    raw_source = (
        "HTTPS://Careers.Example.Test/apply/?utm_source=mail&jobId=REQ-7#questions"
    )
    with database.session_scope() as session:
        record = OpportunityService(session).add(
            Opportunity(
                employer="Acme Capital",
                role_title="Credit Internship",
                cycle="2027",
                url=raw_source,
            )
        )

        assert record.url == (
            "https://careers.example.test/apply/?utm_source=mail&jobId=REQ-7#questions"
        )
        assert record.navigation_url == record.url


def test_unresolved_ingestion_persists_but_is_excluded_from_autopilot(tmp_path: Path) -> None:
    settings, database, crypto = _database(tmp_path)
    row = ScrapedOpportunity(
        employer="Acme Capital",
        role_title="Summer Analyst Internship",
        source_url="https://aggregator.example.test/jobs/acme-summer",
        application_url=None,
        source="brightnetwork",
    )
    with database.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        report = scout.ingest([row], default_cycle="2027")
        opportunity = session.query(Opportunity).one()

        assert report["imported"] == 1
        assert opportunity.url == row.source_url
        assert opportunity.application_url is None
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert scout.autopilot_candidates() == []


def test_official_lever_feed_retains_source_but_does_not_self_attest_apply_url(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _database(tmp_path)
    row = ScrapedOpportunity(
        employer="Acme Capital",
        role_title="Spring Insight Programme",
        source_url="https://jobs.lever.co/acme/abc",
        application_url="https://jobs.lever.co/acme/abc/apply?lever-source=api",
        ats_type="lever",
        source="lever:acme",
    )
    with database.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        scout.ingest([row], default_cycle="2027")
        opportunity = session.query(Opportunity).one()
        application = session.query(Application).one()
        application.state = ApplicationState.PACKAGE_PREPARED.value
        session.flush()

        assert opportunity.url == row.source_url
        assert opportunity.application_url is None
        assert opportunity.automation_url is None
        assert opportunity.target_status == TargetKind.APPLICATION_ENTRY.value
        assert opportunity.resolved_ats_type == "lever"
        evidence = json.loads(opportunity.resolution_evidence_json)
        assert evidence["identity_verified"] is False
        assert scout.autopilot_candidates() == []


def test_record_round_trip_stamps_loader_identity_and_preserves_form_root_proof(
    tmp_path: Path,
) -> None:
    """A production-written verified envelope must be reloadable by both owners."""

    from app.automation.host_policy import origin_for_url
    from app.automation.runner import AutomationRunner
    from app.services.navigator import ApplicationNavigator
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.application_url = (
            "https://boards.greenhouse.io/acme/jobs/202700431"
        )
        session.add(opportunity)
        session.flush()
        final_url = (
            "https://boards.greenhouse.io/acme/jobs/202700431"
            "?src=mail%20merge"
        )
        result = TargetResolution(
            source_url=opportunity.url,
            final_url=final_url,
            kind=TargetKind.APPLICATION_FORM,
            provider="greenhouse",
            identity_verified=True,
            form_verified=True,
            reason_codes=("verified_application_form",),
            evidence={
                "structured_feed": "greenhouse:acme",
                "employer": opportunity.employer,
                "role": opportunity.role_title,
                "requisition": "202700431",
                "form": {
                    "frame_url": final_url,
                    "root_selector": "#apply",
                    "control_count": 1,
                    "submit_present": True,
                    "root_token": "root-application-00431",  # gitleaks:allow -- synthetic DOM-root identity, not an API token
                    "form_identity": "202700431",
                    "binding_verified": True,
                    "bound_target_url": final_url,
                    "bound_provider": "greenhouse",
                    "bound_role": opportunity.role_title,
                    "bound_requisition": "202700431",
                    "bound_form_identity": "202700431",
                    "value": "visible-form-root-marker",
                    "auth_token": "AUTH-SECRET-MUST-NOT-PERSIST",
                },
            },
        )

        recorded = TargetResolutionService(session).record(opportunity.id, result)
        persisted = json.loads(recorded.resolution_evidence_json)
        evidence = persisted["evidence"]
        assert evidence["employer"] == opportunity.employer
        assert evidence["role"] == opportunity.role_title
        assert evidence["application_origin"] == origin_for_url(final_url)
        assert evidence["requisition"] == "202700431"
        assert evidence["form_identity"] == "202700431"
        assert evidence["form"]["root_token"] == "root-application-00431"
        assert evidence["form"]["value"] == "visible-form-root-marker"
        assert "AUTH-SECRET-MUST-NOT-PERSIST" not in recorded.resolution_evidence_json
        assert "202700431" in recorded.resolution_evidence_json

        runner_resolution = AutomationRunner._resolution_from_opportunity(recorded)

        class _Session:
            def get(self, model, identifier):
                if model is Application and identifier == application.id:
                    return application
                if model is Opportunity and identifier == opportunity.id:
                    return opportunity
                return None

        class _Database:
            def session_scope(self):
                from contextlib import contextmanager

                @contextmanager
                def scope():
                    yield _Session()

                return scope()

        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.NEEDS_USER.value,
        )
        navigator = ApplicationNavigator(
            _Database(),
            worker_factory=lambda **kwargs: pytest.fail(
                "round-trip resolution must load before worker creation"
            ),
            headless=True,
        )
        navigator_resolution, _summary = navigator._resolve_target(application.id)
        assert navigator_resolution == runner_resolution


def test_sanitised_requisition_ids_and_encoded_query_urls_are_stable(tmp_path: Path) -> None:
    """Redaction and URL sanitisation must not mutate target identity proof."""

    from app.automation.runner import AutomationRunner
    from app.automation.targets import canonical_target_contract_url
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.application_url = (
            "https://boards.greenhouse.io/acme/jobs/202700431"
        )
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/202700431?src=mail%20merge",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            evidence={
                "structured_feed": "greenhouse:acme",
                "requisition": "202700431",
                "form_identity": "202700431",
                "employer": opportunity.employer,
                "role": opportunity.role_title,
            },
        )
        recorded = TargetResolutionService(session).record(opportunity.id, result)
        persisted = json.loads(recorded.resolution_evidence_json)
        assert persisted["evidence"]["requisition"] == "202700431"
        assert "202700431" in recorded.resolution_evidence_json
        assert canonical_target_contract_url(
            AutomationRunner._resolution_from_opportunity(recorded).final_url
        ) == canonical_target_contract_url(recorded.application_url)


def test_placeholder_employer_is_established_from_verified_provider_identity(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.employer = "Unknown"
        opportunity.application_url = "https://boards.greenhouse.io/acme/jobs/1234"
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/1234",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=True,
            reason_codes=("direct_ats_job",),
            evidence={
                "provider": "greenhouse",
                "employer": "Acme Capital",
                "role": opportunity.role_title,
                "requisition": "1234",
                "application_origin": "https://boards.greenhouse.io",
            },
        )

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            resolver=lambda _context: result,
        )

        assert outcome.promoted is True
        assert opportunity.employer == "Acme Capital"
        assert outcome.application_url == result.final_url
        persisted = json.loads(opportunity.resolution_evidence_json)
        assert persisted["employer_backfill"] == {
            "from_placeholder": "Unknown",
            "established_employer": "Acme Capital",
            "proof": "verified_provider_identity",
        }
        assert "employer_unprovable" not in persisted["reason_codes"]


def test_placeholder_employer_without_provider_proof_has_distinct_reason(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    _settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _unresolved_opportunity()
        opportunity.employer = "   "
        session.add(opportunity)
        session.flush()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url="https://boards.greenhouse.io/acme/jobs/1234",
            kind=TargetKind.APPLICATION_ENTRY,
            provider="greenhouse",
            identity_verified=False,
            reason_codes=("direct_ats_job",),
            evidence={"role": opportunity.role_title, "requisition": "1234"},
        )

        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            resolver=lambda _context: result,
        )

        assert outcome.promoted is False
        assert opportunity.employer == "   "
        assert "employer_unprovable" in outcome.reason_codes
        assert "target_not_verified" not in outcome.reason_codes
