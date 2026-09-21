"""Scout service: ingest scraped opportunities into ARGUS and drive the
autopilot pipeline.

Ingestion: scraped opportunities -> Opportunity rows (deduped) -> Application
rows in DISCOVERED state, each tagged with its programme type.

Autopilot: for every application eligible for automation, run the full
ARGUS pipeline (evaluate -> queue -> prepare -> guarded run). The pipeline
STOPS for anything a human must legally do: CAPTCHA, legal declarations,
demographic questions, assessments. Those land in NEEDS_USER / NEEDS_OA.

Programme priority logic:
  - Year in Industry and Spring Weeks: always auto-processed.
  - Summer: auto-processed ONLY if the employer has no open Year in Industry
    (user's fallback rule).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from fnmatch import fnmatchcase
from typing import Iterable, Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.opportunity_scope import out_of_scope_programme_reason
from app.domain.states import (
    ApplicationState,
    UserApplicationStatus,
    user_status_is_automation_eligible,
    user_status_work_rank,
)
from app.domain.targets import TargetKind, source_fingerprint
from app.models import (
    Application,
    AutomationRun,
    ConflictRule,
    Opportunity,
    OpportunityArchive,
)
from app.repositories import ApplicationRepository
from app.scouting.divisions import infer_division
from app.scouting.application_window import (
    ApplicationWindowStatus,
    coerce_application_window,
    derive_application_window,
    tracker_owned_host,
)
from app.scouting.programmes import (
    AUTO_APPLY_PRIORITY,
    ProgrammeType,
    classify_programme,
    should_auto_apply,
)
from app.scouting.trackr_identity import (
    IdentityCandidate,
    TrackrIdentityConflictError,
    TrackrIdentityDisposition,
    TrackrIdentityPlan,
    material_identity_signature,
    normalize_trackr_id,
    plan_trackr_identities,
)
from app.security.audit import AuditInput, append_audit
from app.services.applications import (
    ApplicationBlockedError,
    ApplicationService,
)
from app.domain.targets import validate_navigation_url
from app.services.opportunities import OpportunityService
from app.automation.targets import classify_target
from app.services.target_resolution import TargetResolutionService
from app.security.crypto import CryptoBox

logger = logging.getLogger(__name__)


def _trusted_provider_hint(candidate_url: str) -> tuple[str, str] | None:
    """Return a provider hint only for an exact provider-controlled host."""

    try:
        navigation_url = validate_navigation_url(candidate_url)
    except ValueError:
        return None
    host = (urlsplit(navigation_url).hostname or "").casefold()
    provider = {
        "boards.greenhouse.io": "greenhouse",
        "job-boards.greenhouse.io": "greenhouse",
        "job-boards.eu.greenhouse.io": "greenhouse",
        "jobs.lever.co": "lever",
    }.get(host)
    return (provider, navigation_url) if provider else None

OPEN_STATES = {
    ApplicationState.DISCOVERED.value,
    ApplicationState.ELIGIBILITY_CHECKED.value,
    ApplicationState.QUEUED.value,
    ApplicationState.PACKAGE_PREPARED.value,
    ApplicationState.FILLING.value,
    ApplicationState.FAILED_RETRYABLE.value,
    ApplicationState.READY_TO_SUBMIT.value,
}

_YII_SUPPRESSING_STATES = OPEN_STATES | {
    ApplicationState.NEEDS_USER.value,
    ApplicationState.NEEDS_OA.value,
}
_SUBMISSION_TERMINAL_STATES = {
    ApplicationState.SUBMITTED.value,
    ApplicationState.CONFIRMATION_VERIFIED.value,
}
# Target kinds the no-login skip reports without ever running. The runnable
# candidate query already excludes them via Opportunity.automation_url; this
# tuple lets run_autopilot name them in skip counts/details instead of
# leaving correctly classified walls invisible.
_LOGIN_WALL_SKIP_KINDS = (
    TargetKind.AUTH_WALL.value,
    TargetKind.HUMAN_CHALLENGE.value,
)
_USABLE_EXPLICIT_PROGRAMME_TYPES = frozenset(
    {
        ProgrammeType.YEAR_IN_INDUSTRY,
        ProgrammeType.SPRING_WEEK,
        ProgrammeType.SUMMER,
    }
)
_CLOSED_WINDOW_ARCHIVE_REASON = "closed_application_window"
_AMBIGUOUS_TRACKR_IDENTITY_ARCHIVE_REASON = "ambiguous_trackr_identity"


class IngestibleOpportunity(Protocol):
    """Structural contract shared by saved/aggregator and live Trackr rows."""

    employer: str
    role_title: str
    url: str
    location: str
    source: str
    ats_type: str
    deadline: date | None


def _validated_candidate_application_url(item: IngestibleOpportunity) -> str | None:
    raw = str(getattr(item, "application_url", "") or "").strip()
    if not raw or tracker_owned_host(raw):
        return None
    try:
        return validate_navigation_url(raw)
    except ValueError:
        return None


def _ingestion_window_status(
    item: IngestibleOpportunity,
    *,
    source_url: str,
    application_url: str | None,
) -> ApplicationWindowStatus:
    supplied = getattr(item, "application_window_status", None)
    if supplied is not None:
        return coerce_application_window(supplied)
    source = str(getattr(item, "source", "") or "")
    is_trackr_live = source.startswith("trackr_live")
    inference_url = application_url
    if inference_url is None and not tracker_owned_host(source_url):
        inference_url = source_url
    return derive_application_window(
        explicit_status=getattr(item, "explicit_status", None),
        opening_date=getattr(item, "opening_date", None),
        closing_date=getattr(item, "closing_date", None) or getattr(item, "deadline", None),
        application_url=inference_url,
        link_signal_known=is_trackr_live or bool(source_url),
    )


def _verified_target(record: Opportunity) -> bool:
    try:
        kind = TargetKind(record.target_status)
    except ValueError:
        return False
    return bool(
        kind.automation_eligible
        and record.application_url
        and record.resolved_at is not None
    )


def _ingestion_programme_type(item: IngestibleOpportunity) -> ProgrammeType:
    """Prefer a supported scraped category; otherwise classify title evidence."""

    try:
        explicit_type = ProgrammeType(getattr(item, "programme_type", None))
    except (TypeError, ValueError):
        explicit_type = ProgrammeType.OTHER
    if explicit_type in _USABLE_EXPLICIT_PROGRAMME_TYPES:
        return explicit_type
    return classify_programme(item.role_title, item.employer)


class ScoutService:
    def __init__(self, session: Session, settings: Settings, crypto: CryptoBox):
        self.session = session
        self.settings = settings
        self.crypto = crypto

    # ------------------------------------------------------------------ ingest

    def ingest(
        self,
        scraped: Iterable[IngestibleOpportunity],
        *,
        default_cycle: str = "2026-27",
        allow_trackr_identity_mutation: bool = True,
    ) -> dict[str, int]:
        """Insert or safely refresh opportunities without deleting inventory."""
        supplied_items = tuple(scraped)
        if allow_trackr_identity_mutation:
            items, identity_plan = self._plan_trackr_identity_batch(
                supplied_items, default_cycle=default_cycle
            )
        else:
            items = supplied_items
            identity_plan = TrackrIdentityPlan((), (), ())
        stats = {
            "seen": 0,
            "imported": 0,
            "updated": 0,
            "duplicates": 0,
            "excluded": 0,
            "applications": 0,
            "out_of_scope_programme": 0,
            "unknown_programme": 0,
            "missing_url": 0,
            "invalid_url": 0,
            "ingestion_failure": 0,
            "window_open": 0,
            "window_not_yet_open": 0,
            "window_closed": 0,
            "window_unknown": 0,
            "without_employer_link": 0,
            "not_yet_open_no_link": 0,
            "invalid_application_urls": 0,
            "application_urls_captured": 0,
            "application_urls_backfilled": 0,
            "verified_targets_preserved": 0,
            "legacy_sources_migrated": 0,
            "closed_archived": 0,
            "closed_unarchived": 0,
            "trackr_ids_seen": 0,
            "trackr_ids_missing_or_invalid": 0,
            "trackr_id_matched": 0,
            "trackr_id_legacy_claimed": 0,
            "trackr_id_legacy_ambiguous": len(identity_plan.ambiguous_raw_ids),
            "trackr_id_inserted": 0,
            "trackr_id_conflict_recovered": 0,
            "trackr_id_conflicting_duplicates": 0,
            "trackr_ambiguous_legacy_archived": 0,
            "trackr_ambiguous_legacy_preserved": 0,
            "trackr_ambiguous_legacy_deferred": 0,
            "trackr_identity_owner_mismatch": 0,
            "trackr_identity_disabled_unmatched": 0,
        }
        identity_by_id = identity_plan.by_raw_id
        represented_trackr_ids: set[str] = set()
        service = OpportunityService(self.session)
        for item in items:
            stats["seen"] += 1
            source_label = str(getattr(item, "source", "trackr_live") or "")[:120]
            is_trackr_live = source_label.startswith("trackr_live")
            raw_trackr_programme_id = (
                normalize_trackr_id(getattr(item, "source_record_id", ""))
                if is_trackr_live
                else ""
            )
            trackr_programme_id = (
                raw_trackr_programme_id
                if allow_trackr_identity_mutation
                else ""
            )
            if is_trackr_live:
                if raw_trackr_programme_id:
                    stats["trackr_ids_seen"] += 1
                else:
                    stats["trackr_ids_missing_or_invalid"] += 1
            scope_reason = out_of_scope_programme_reason(
                item.role_title,
                str(getattr(item, "programme_type", "") or ""),
            )
            if scope_reason is not None:
                stats["excluded"] += 1
                stats["out_of_scope_programme"] += 1
                continue
            programme_type = _ingestion_programme_type(item)
            if programme_type is ProgrammeType.OTHER:
                # keep graduate schemes etc. out of the pipeline entirely
                stats["excluded"] += 1
                stats["unknown_programme"] += 1
                continue
            raw_source_url = str(getattr(item, "source_url", "") or item.url or "")
            if not raw_source_url:
                stats["excluded"] += 1
                stats["missing_url"] += 1
                continue
            try:
                url = validate_navigation_url(raw_source_url)
            except ValueError:
                stats["excluded"] += 1
                stats["invalid_url"] += 1
                continue
            raw_candidate_application_url = str(
                getattr(item, "application_url", "") or ""
            ).strip()
            candidate_application_url = _validated_candidate_application_url(item)
            if raw_candidate_application_url and candidate_application_url is None:
                stats["invalid_application_urls"] += 1
            elif bool(getattr(item, "invalid_application_url", False)):
                stats["invalid_application_urls"] += 1
            if is_trackr_live:
                if candidate_application_url is None:
                    stats["without_employer_link"] += 1
                else:
                    stats["application_urls_captured"] += 1
            window_status = _ingestion_window_status(
                item,
                source_url=url,
                application_url=candidate_application_url,
            )
            stats[f"window_{window_status.value.casefold()}"] += 1
            if (
                window_status is ApplicationWindowStatus.NOT_YET_OPEN
                and candidate_application_url is None
            ):
                stats["not_yet_open_no_link"] += 1
            opening_date = getattr(item, "opening_date", None)
            closing_date = getattr(item, "closing_date", None) or item.deadline
            source_rolling = getattr(item, "rolling", None)
            rolling = source_rolling is True
            division = (
                str(getattr(item, "division", "") or "").strip()
                or infer_division(item.role_title, item.employer)
            )
            try:
                with self.session.begin_nested():
                    candidate = Opportunity(
                        employer=item.employer,
                        role_title=item.role_title,
                        # CV tag matching keys off this slug; scrape what the
                        # source provides, otherwise infer from the title.
                        division=division,
                        programme_group=programme_type.value,
                        location=item.location,
                        cycle=default_cycle,
                        url=url,
                        source=source_label,
                        ats_type=item.ats_type,
                        opening_date=opening_date,
                        deadline=closing_date,
                        rolling=rolling,
                        application_window_status=window_status.value,
                        trackr_programme_id=trackr_programme_id or None,
                        application_url=(
                            candidate_application_url if is_trackr_live else None
                        ),
                        target_status=(
                            TargetKind.UNRESOLVED.value
                            if candidate_application_url or not is_trackr_live
                            else TargetKind.MISSING_EMPLOYER_LINK.value
                        ),
                    )
                    planned_identity = (
                        identity_by_id.get(trackr_programme_id)
                        if trackr_programme_id
                        else None
                    )
                    record = None
                    legacy_source = False
                    identity_claimed = False
                    identity_inserted_this_row = False
                    bound_identity_collision = False
                    if (
                        not allow_trackr_identity_mutation
                        and raw_trackr_programme_id
                    ):
                        # Window backfill may refresh an already-bound row, but
                        # it never creates, claims, or changes an identity.
                        record = self.session.scalar(
                            select(Opportunity).where(
                                Opportunity.trackr_programme_id
                                == raw_trackr_programme_id
                            )
                        )
                    if trackr_programme_id and planned_identity is None:
                        raise RuntimeError(
                            "identified Trackr row was not present in the batch plan"
                        )
                    if planned_identity is not None:
                        if planned_identity.disposition in {
                            TrackrIdentityDisposition.EXISTING_BOUND,
                            TrackrIdentityDisposition.CLAIM_LEGACY,
                        }:
                            record = self.session.get(
                                Opportunity, planned_identity.opportunity_id
                            )
                            if record is None:
                                raise RuntimeError(
                                    "planned Trackr opportunity disappeared: "
                                    f"{planned_identity.opportunity_id}"
                                )
                            if (
                                planned_identity.disposition
                                is TrackrIdentityDisposition.EXISTING_BOUND
                            ):
                                if record.trackr_programme_id != trackr_programme_id:
                                    raise RuntimeError(
                                        "persisted Trackr identity changed after planning"
                                    )
                                stats["trackr_id_matched"] += 1
                            else:
                                if record.trackr_programme_id not in {
                                    None,
                                    trackr_programme_id,
                                }:
                                    raise RuntimeError(
                                        "refusing to overwrite a different Trackr identity"
                                    )
                                if record.trackr_programme_id is None:
                                    record.trackr_programme_id = trackr_programme_id
                                    append_audit(
                                        self.session,
                                        AuditInput(
                                            "scout",
                                            "scout.trackr_identity_claimed",
                                            "opportunity",
                                            record.id,
                                            {
                                                "trackr_programme_id": trackr_programme_id,
                                                "source": source_label,
                                                "disposition": "claim_legacy",
                                                "previous_trackr_programme_id": None,
                                            },
                                        ),
                                    )
                                    identity_claimed = True
                                stats["trackr_id_legacy_claimed"] += 1
                    elif record is None:
                        record = service.find_exact(candidate)
                        if (
                            is_trackr_live
                            and record is not None
                            and record.trackr_programme_id is not None
                        ):
                            bound_identity_collision = True
                            record = None
                    if (
                        record is None
                        and is_trackr_live
                        and not trackr_programme_id
                    ):
                        unbound_identity_filter = (
                            Opportunity.trackr_programme_id.is_(None)
                        )
                        if candidate_application_url:
                            legacy_candidate = Opportunity(
                                employer=item.employer,
                                role_title=item.role_title,
                                division=division,
                                programme_group=programme_type.value,
                                location=item.location,
                                cycle=default_cycle,
                                url=candidate_application_url,
                                source=source_label,
                                ats_type=item.ats_type,
                                opening_date=opening_date,
                                deadline=closing_date,
                                rolling=rolling,
                                application_window_status=window_status.value,
                            )
                            record = service.find_exact(legacy_candidate)
                            if (
                                record is not None
                                and record.trackr_programme_id is not None
                            ):
                                bound_identity_collision = True
                                record = None
                            if record is None:
                                legacy_matches = list(
                                    self.session.scalars(
                                        select(Opportunity).where(
                                            Opportunity.employer == item.employer,
                                            Opportunity.role_title == item.role_title,
                                            Opportunity.cycle == default_cycle,
                                            Opportunity.url == candidate_application_url,
                                            Opportunity.source.like("trackr_live%"),
                                            unbound_identity_filter,
                                        )
                                    ).all()
                                )
                                if len(legacy_matches) == 1:
                                    record = legacy_matches[0]
                        if record is None:
                            exact_title_matches = list(
                                self.session.scalars(
                                    select(Opportunity).where(
                                        Opportunity.employer == item.employer,
                                        Opportunity.role_title == item.role_title,
                                        Opportunity.cycle == default_cycle,
                                        Opportunity.source.like("trackr_live%"),
                                        unbound_identity_filter,
                                    )
                                ).all()
                            )
                            if len(exact_title_matches) == 1:
                                record = exact_title_matches[0]
                        if record is None:
                            unique_programme_matches = list(
                                self.session.scalars(
                                    select(Opportunity).where(
                                        Opportunity.employer == item.employer,
                                        Opportunity.cycle == default_cycle,
                                        Opportunity.programme_group
                                        == programme_type.value,
                                        Opportunity.source.like("trackr_live%"),
                                        unbound_identity_filter,
                                    )
                                ).all()
                            )
                            if len(unique_programme_matches) == 1:
                                record = unique_programme_matches[0]
                        legacy_source = record is not None
                    if record is None and (
                        bound_identity_collision
                        or (
                            not allow_trackr_identity_mutation
                            and bool(raw_trackr_programme_id)
                        )
                    ):
                        stats["excluded"] += 1
                        if bound_identity_collision:
                            stats["trackr_identity_owner_mismatch"] += 1
                        if (
                            not allow_trackr_identity_mutation
                            and raw_trackr_programme_id
                        ):
                            stats["trackr_identity_disabled_unmatched"] += 1
                        continue
                    if record is not None:
                        if not is_trackr_live:
                            stats["duplicates"] += 1
                            continue
                        changed = identity_claimed
                        previous_window = str(record.application_window_status or "")
                        previous_application_url = record.application_url
                        if trackr_programme_id:
                            # The persisted provider ID is authoritative for
                            # mutable listing evidence.  The historical source
                            # fingerprint is intentionally never recomputed.
                            identity_refresh_values = {
                                "employer": item.employer,
                                "role_title": item.role_title,
                                "programme_group": programme_type.value,
                                "location": item.location,
                                "division": division,
                                "url": url,
                            }
                            for attribute, value in identity_refresh_values.items():
                                if getattr(record, attribute) != value:
                                    setattr(record, attribute, value)
                                    changed = True
                        if legacy_source:
                            record.employer = item.employer
                            record.role_title = item.role_title
                            record.programme_group = programme_type.value
                            if item.location:
                                record.location = item.location
                            record.division = division
                            record.url = url
                            record.source_fingerprint = source_fingerprint(
                                employer=record.employer,
                                role_title=record.role_title,
                                cycle=record.cycle,
                                source_url=url,
                                location=record.location,
                                division=record.division,
                            )
                            stats["legacy_sources_migrated"] += 1
                            changed = True
                        refresh_values = {
                            "source": source_label,
                            "ats_type": str(item.ats_type or "unknown"),
                            "opening_date": opening_date,
                            "deadline": closing_date,
                            "rolling": rolling,
                            "application_window_status": window_status.value,
                        }
                        for attribute, value in refresh_values.items():
                            if getattr(record, attribute) != value:
                                setattr(record, attribute, value)
                                changed = True
                        if _verified_target(record):
                            if (
                                candidate_application_url
                                and record.application_url != candidate_application_url
                            ):
                                stats["verified_targets_preserved"] += 1
                        else:
                            next_application_url = (
                                candidate_application_url
                                if is_trackr_live
                                else record.application_url
                            )
                            if record.application_url != next_application_url:
                                if record.application_url is None and next_application_url:
                                    stats["application_urls_backfilled"] += 1
                                record.application_url = next_application_url
                                changed = True
                            if candidate_application_url:
                                if record.target_status == TargetKind.MISSING_EMPLOYER_LINK.value:
                                    record.target_status = TargetKind.UNRESOLVED.value
                                    record.resolution_attempted_at = None
                                    changed = True
                            elif record.target_status != TargetKind.MISSING_EMPLOYER_LINK.value:
                                record.target_status = TargetKind.MISSING_EMPLOYER_LINK.value
                                record.resolution_attempted_at = None
                                changed = True
                        archive_change = self._sync_window_archive(record, window_status)
                        if archive_change == "archived":
                            stats["closed_archived"] += 1
                            changed = True
                        elif archive_change == "unarchived":
                            stats["closed_unarchived"] += 1
                            changed = True
                        # Existing-bound and safely claimed rows are metadata-
                        # only. Preserve their exact existing child set.
                        application = self.session.scalar(
                            select(Application).where(
                                Application.opportunity_id == record.id
                            )
                        )
                        if application is not None:
                            priority = self._scout_priority(record)
                            if application.priority != priority:
                                application.priority = priority
                                changed = True
                        if changed:
                            stats["updated"] += 1
                            append_audit(
                                self.session,
                                AuditInput(
                                    "scout",
                                    "scout.opportunity_refreshed",
                                    "opportunity",
                                    record.id,
                                    {
                                        "source": source_label,
                                        "previous_window": previous_window,
                                        "application_window_status": window_status.value,
                                        "application_url_backfilled": bool(
                                            previous_application_url is None
                                            and record.application_url
                                        ),
                                        "legacy_source_migrated": legacy_source,
                                        "trackr_programme_id": (
                                            record.trackr_programme_id
                                        ),
                                        "identity_claimed": identity_claimed,
                                    },
                                ),
                            )
                        else:
                            stats["duplicates"] += 1
                        if trackr_programme_id:
                            represented_trackr_ids.add(trackr_programme_id)
                        continue

                    if trackr_programme_id:
                        try:
                            with self.session.begin_nested():
                                self.session.add(candidate)
                                self.session.flush()
                            record = candidate
                            identity_inserted_this_row = True
                        except IntegrityError:
                            record = self.session.scalar(
                                select(Opportunity).where(
                                    Opportunity.trackr_programme_id
                                    == trackr_programme_id
                                )
                            )
                            if record is None:
                                raise
                            stats["trackr_id_conflict_recovered"] += 1
                            stats["trackr_id_matched"] += 1
                            self._refresh_identity_race_record(
                                record,
                                candidate,
                                candidate_application_url=candidate_application_url,
                                window_status=window_status,
                                stats=stats,
                            )
                            represented_trackr_ids.add(trackr_programme_id)
                            continue
                        append_audit(
                            self.session,
                            AuditInput(
                                "scout",
                                "opportunity.created",
                                "opportunity",
                                record.id,
                                {
                                    "employer": record.employer,
                                    "role": record.role_title,
                                    "source": record.source,
                                },
                            ),
                        )
                        append_audit(
                            self.session,
                            AuditInput(
                                "scout",
                                "scout.trackr_identity_inserted",
                                "opportunity",
                                record.id,
                                {
                                    "trackr_programme_id": trackr_programme_id,
                                    "source": source_label,
                                    "disposition": "insert_new",
                                },
                            ),
                        )
                    else:
                        record = service.add(candidate)
                    if not is_trackr_live:
                        trusted_candidate = _trusted_provider_hint(
                            raw_candidate_application_url
                        )
                    else:
                        trusted_candidate = None
                    if trusted_candidate is not None:
                        provider_hint, navigation_candidate = trusted_candidate
                        try:
                            resolution = classify_target(
                                record.url,
                                navigation_candidate,
                                provider_hint=provider_hint,
                                identity_verified=False,
                                evidence={
                                    "trusted_destination_host": (
                                        urlsplit(navigation_candidate).hostname or ""
                                    ).casefold(),
                                    "structured_candidate": True,
                                },
                            )
                            TargetResolutionService(self.session).record(
                                record.id, resolution
                            )
                        except ValueError as exc:
                            logger.warning(
                                "structured target ignored for %s: %s", record.id, exc
                            )
                    repository = ApplicationRepository(self.session)
                    application, application_created = (
                        repository.get_or_create_for_opportunity(
                            record.id,
                            state=ApplicationState.DISCOVERED.value,
                            priority=self._scout_priority(record),
                        )
                    )
                    if not application_created:
                        raise RuntimeError(
                            "newly inserted opportunity already had an application"
                        )
                    append_audit(
                        self.session,
                        AuditInput(
                            "scout",
                            "scout.opportunity_ingested",
                            "opportunity",
                            record.id,
                            {
                                "programme_type": programme_type,
                                "source": source_label,
                                "application_id": application.id,
                                "application_window_status": window_status.value,
                            },
                        ),
                    )
                    archive_change = self._sync_window_archive(record, window_status)
                    if archive_change == "archived":
                        stats["closed_archived"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.warning("ingest failed for %s: %s", item.url, exc)
                stats["ingestion_failure"] += 1
                continue
            if identity_inserted_this_row:
                stats["trackr_id_inserted"] += 1
                represented_trackr_ids.add(trackr_programme_id)
            stats["imported"] += 1
            stats["applications"] += 1
        ambiguous_ids = set(identity_plan.ambiguous_raw_ids)
        ambiguity_complete = bool(ambiguous_ids) and ambiguous_ids <= represented_trackr_ids
        if ambiguity_complete:
            self.session.flush()
            represented_counts = {
                raw_id: int(
                    self.session.scalar(
                        select(func.count(Opportunity.id)).where(
                            Opportunity.trackr_programme_id == raw_id
                        )
                    )
                    or 0
                )
                for raw_id in ambiguous_ids
            }
            ambiguity_complete = all(
                count == 1 for count in represented_counts.values()
            )
        if ambiguity_complete:
            for opportunity_id in identity_plan.ambiguous_legacy_opportunity_ids:
                record = self.session.get(Opportunity, opportunity_id)
                if record is None:
                    raise RuntimeError(
                        "planned ambiguous Trackr legacy opportunity disappeared: "
                        f"{opportunity_id}"
                    )
                archive_change = self._sync_identity_archive(record)
                if archive_change == "archived":
                    stats["trackr_ambiguous_legacy_archived"] += 1
                elif archive_change == "preserved":
                    stats["trackr_ambiguous_legacy_preserved"] += 1
        elif identity_plan.ambiguous_legacy_opportunity_ids:
            stats["trackr_ambiguous_legacy_deferred"] = len(
                identity_plan.ambiguous_legacy_opportunity_ids
            )
        self.session.flush()
        return stats

    def _plan_trackr_identity_batch(
        self,
        items: tuple[IngestibleOpportunity, ...],
        *,
        default_cycle: str,
    ) -> tuple[tuple[IngestibleOpportunity, ...], TrackrIdentityPlan]:
        """Validate duplicate IDs and plan every eligible identified row."""

        material_by_id: dict[str, tuple[object, ...]] = {}
        deduplicated: list[IngestibleOpportunity] = []
        for item in items:
            source = str(getattr(item, "source", "") or "")[:120]
            raw_id = (
                normalize_trackr_id(getattr(item, "source_record_id", ""))
                if source.startswith("trackr_live")
                else ""
            )
            if raw_id:
                signature = material_identity_signature(item)
                previous = material_by_id.get(raw_id)
                if previous is not None:
                    if previous != signature:
                        raise TrackrIdentityConflictError(
                            "conflicting ingest rows share Trackr programme ID "
                            f"{raw_id!r}"
                        )
                    continue
                material_by_id[raw_id] = signature
            deduplicated.append(item)

        incoming: list[IdentityCandidate] = []
        for item in deduplicated:
            source = str(getattr(item, "source", "") or "")[:120]
            raw_id = normalize_trackr_id(getattr(item, "source_record_id", ""))
            if not source.startswith("trackr_live") or not raw_id:
                continue
            if out_of_scope_programme_reason(
                item.role_title,
                str(getattr(item, "programme_type", "") or ""),
            ) is not None:
                continue
            programme_type = _ingestion_programme_type(item)
            if programme_type is ProgrammeType.OTHER:
                continue
            raw_source_url = str(
                getattr(item, "source_url", "") or getattr(item, "url", "") or ""
            )
            try:
                tracker_url = validate_navigation_url(raw_source_url)
            except ValueError:
                continue
            division = (
                str(getattr(item, "division", "") or "").strip()
                or infer_division(item.role_title, item.employer)
            )
            incoming.append(
                IdentityCandidate(
                    raw_id=raw_id,
                    employer=item.employer,
                    role_title=item.role_title,
                    cycle=default_cycle,
                    source=source,
                    tracker_url=tracker_url,
                    programme_group=programme_type.value,
                    location=item.location,
                    division=division,
                )
            )

        if not incoming:
            return tuple(deduplicated), TrackrIdentityPlan((), (), ())

        raw_ids = tuple(candidate.raw_id for candidate in incoming)
        bound_records = list(
            self.session.scalars(
                select(Opportunity).where(
                    Opportunity.trackr_programme_id.in_(raw_ids)
                )
            ).all()
        )
        bound_by_id = {
            str(record.trackr_programme_id): record.id for record in bound_records
        }
        exact_sources = tuple(
            sorted(
                {
                    candidate.source
                    for candidate in incoming
                    if candidate.source.startswith("trackr_live:")
                }
            )
        )
        tracker_urls = tuple(sorted({candidate.tracker_url for candidate in incoming}))
        legacy_records: list[Opportunity] = []
        if exact_sources and tracker_urls:
            legacy_records = list(
                self.session.scalars(
                    select(Opportunity).where(
                        Opportunity.trackr_programme_id.is_(None),
                        Opportunity.cycle == default_cycle,
                        Opportunity.source.in_(exact_sources),
                        Opportunity.url.in_(tracker_urls),
                    )
                ).all()
            )
        legacy = [
            IdentityCandidate(
                raw_id="",
                employer=record.employer,
                role_title=record.role_title,
                cycle=record.cycle,
                source=record.source,
                tracker_url=record.url,
                programme_group=record.programme_group,
                location=record.location,
                division=record.division,
                opportunity_id=record.id,
            )
            for record in legacy_records
        ]
        return (
            tuple(deduplicated),
            plan_trackr_identities(incoming, bound_by_id, legacy),
        )

    def _sync_identity_archive(self, opportunity: Opportunity) -> str:
        """Archive an ambiguous legacy row without overwriting prior reasons."""

        marker = opportunity.archive_record
        if marker is not None and marker.archived_at is not None:
            return "preserved"
        if marker is None:
            marker = OpportunityArchive(
                opportunity=opportunity,
                archived_at=datetime.now(timezone.utc),
                archived_reason=_AMBIGUOUS_TRACKR_IDENTITY_ARCHIVE_REASON,
            )
            self.session.add(marker)
        else:
            marker.archived_at = datetime.now(timezone.utc)
            marker.archived_reason = _AMBIGUOUS_TRACKR_IDENTITY_ARCHIVE_REASON
        append_audit(
            self.session,
            AuditInput(
                "scout",
                "opportunity.archived",
                "opportunity",
                opportunity.id,
                {"reason": _AMBIGUOUS_TRACKR_IDENTITY_ARCHIVE_REASON},
            ),
        )
        return "archived"

    def _refresh_identity_race_record(
        self,
        record: Opportunity,
        candidate: Opportunity,
        *,
        candidate_application_url: str | None,
        window_status: ApplicationWindowStatus,
        stats: dict[str, int],
    ) -> None:
        """Converge on the same provider ID after a uniqueness race."""

        if record.trackr_programme_id != candidate.trackr_programme_id:
            raise RuntimeError("Trackr identity uniqueness race resolved to wrong row")
        changed = False
        for attribute in (
            "employer",
            "role_title",
            "programme_group",
            "location",
            "division",
            "url",
            "source",
            "ats_type",
            "opening_date",
            "deadline",
            "rolling",
            "application_window_status",
        ):
            value = getattr(candidate, attribute)
            if getattr(record, attribute) != value:
                setattr(record, attribute, value)
                changed = True
        if _verified_target(record):
            if candidate_application_url and record.application_url != candidate_application_url:
                stats["verified_targets_preserved"] += 1
        else:
            if record.application_url != candidate_application_url:
                if record.application_url is None and candidate_application_url:
                    stats["application_urls_backfilled"] += 1
                record.application_url = candidate_application_url
                changed = True
            expected_target = (
                TargetKind.UNRESOLVED.value
                if candidate_application_url
                else TargetKind.MISSING_EMPLOYER_LINK.value
            )
            if record.target_status != expected_target:
                record.target_status = expected_target
                record.resolution_attempted_at = None
                changed = True
        archive_change = self._sync_window_archive(record, window_status)
        if archive_change == "archived":
            stats["closed_archived"] += 1
            changed = True
        elif archive_change == "unarchived":
            stats["closed_unarchived"] += 1
            changed = True
        application = self.session.scalar(
            select(Application).where(Application.opportunity_id == record.id)
        )
        if application is not None:
            priority = self._scout_priority(record)
            if application.priority != priority:
                application.priority = priority
                changed = True
        if changed:
            stats["updated"] += 1
            append_audit(
                self.session,
                AuditInput(
                    "scout",
                    "scout.opportunity_refreshed",
                    "opportunity",
                    record.id,
                    {
                        "source": record.source,
                        "application_window_status": record.application_window_status,
                        "trackr_programme_id": record.trackr_programme_id,
                        "identity_conflict_recovered": True,
                    },
                ),
            )
        else:
            stats["duplicates"] += 1

    def _sync_window_archive(
        self,
        opportunity: Opportunity,
        window_status: ApplicationWindowStatus,
    ) -> str:
        """Apply/reverse only the Phase 19 archive reason."""

        marker = opportunity.archive_record
        if window_status is ApplicationWindowStatus.CLOSED:
            if marker is None:
                marker = OpportunityArchive(
                    opportunity=opportunity,
                    archived_at=datetime.now(timezone.utc),
                    archived_reason=_CLOSED_WINDOW_ARCHIVE_REASON,
                )
                self.session.add(marker)
            elif marker.archived_at is None:
                marker.archived_at = datetime.now(timezone.utc)
                marker.archived_reason = _CLOSED_WINDOW_ARCHIVE_REASON
            elif marker.archived_reason != _CLOSED_WINDOW_ARCHIVE_REASON:
                return ""
            else:
                return ""
            append_audit(
                self.session,
                AuditInput(
                    "scout",
                    "opportunity.archived",
                    "opportunity",
                    opportunity.id,
                    {"reason": _CLOSED_WINDOW_ARCHIVE_REASON},
                ),
            )
            return "archived"
        if (
            marker is not None
            and marker.archived_at is not None
            and marker.archived_reason == _CLOSED_WINDOW_ARCHIVE_REASON
        ):
            marker.archived_at = None
            marker.archived_reason = None
            append_audit(
                self.session,
                AuditInput(
                    "scout",
                    "opportunity.unarchived",
                    "opportunity",
                    opportunity.id,
                    {"reason": _CLOSED_WINDOW_ARCHIVE_REASON},
                ),
            )
            return "unarchived"
        return ""

    def backfill_unknown_application_windows(self) -> dict[str, int]:
        """Classify residual legacy rows only from evidence already persisted.

        A non-Trackr URL is positive link evidence. Trackr-owned inventory that
        was not present in the fresh payload remains UNKNOWN rather than being
        guessed open, waiting, or closed.
        """

        stats = {
            "opened": 0,
            "not_yet_open": 0,
            "closed": 0,
            "left_unknown": 0,
            "rolling_cleared": 0,
        }
        records = list(
            self.session.scalars(
                select(Opportunity).where(
                    Opportunity.application_window_status
                    == ApplicationWindowStatus.UNKNOWN.value
                )
            ).all()
        )
        for record in records:
            rolling_cleared = False
            if record.rolling:
                record.rolling = False
                stats["rolling_cleared"] += 1
                rolling_cleared = True
            candidate_url = str(record.application_url or "").strip()
            if not candidate_url and not tracker_owned_host(record.url):
                candidate_url = record.url
            state = derive_application_window(
                opening_date=record.opening_date,
                closing_date=record.deadline,
                application_url=candidate_url or None,
                link_signal_known=False,
            )
            archive_change = ""
            if state is ApplicationWindowStatus.UNKNOWN:
                stats["left_unknown"] += 1
            else:
                record.application_window_status = state.value
                archive_change = self._sync_window_archive(record, state)
                stats[
                    {
                        ApplicationWindowStatus.OPEN: "opened",
                        ApplicationWindowStatus.NOT_YET_OPEN: "not_yet_open",
                        ApplicationWindowStatus.CLOSED: "closed",
                    }[state]
                ] += 1
            if rolling_cleared or state is not ApplicationWindowStatus.UNKNOWN:
                evidence = (
                    "rolling_default_cleared"
                    if rolling_cleared and state is ApplicationWindowStatus.UNKNOWN
                    else "persisted_non_trackr_url_or_date"
                )
                append_audit(
                    self.session,
                    AuditInput(
                        "scout",
                        "scout.application_window_backfilled",
                        "opportunity",
                        record.id,
                        {
                            "application_window_status": state.value,
                            "archive_change": archive_change,
                            "evidence": evidence,
                            "rolling_cleared": rolling_cleared,
                        },
                    ),
                )
        self.session.flush()
        return stats

    @staticmethod
    def _scout_priority(opportunity: Opportunity) -> int:
        base = AUTO_APPLY_PRIORITY.get(opportunity.programme_group, 9) * 10
        score = 30 + base  # 30 Yii, 40 spring, 50 summer
        if opportunity.deadline:
            days = (opportunity.deadline - date.today()).days
            # Lower values are processed first.  The old implementation added
            # urgency points, accidentally moving a near deadline later in the
            # queue; subtract instead and keep the score bounded.
            if days <= 7:
                score -= 25
            elif days <= 21:
                score -= 15
        return max(0, min(score, 100))

    # --------------------------------------------------------------- autopilot

    def _employer_has_open_yii(self, employer: str) -> bool:
        # The application session intentionally disables autoflush for the
        # runner's long-lived transaction; make an explicit state/deadline
        # update visible before querying suppression records.
        self.session.flush()
        rows = self.session.execute(
            select(
                Application.state,
                Opportunity.deadline,
                Opportunity.role_title,
                Opportunity.programme_group,
            )
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(
                Opportunity.employer == employer,
                Opportunity.programme_group == ProgrammeType.YEAR_IN_INDUSTRY.value,
                Opportunity.application_window_status
                == ApplicationWindowStatus.OPEN.value,
                Opportunity.user_status.in_(
                    (
                        UserApplicationStatus.INTERESTED.value,
                        UserApplicationStatus.NOT_APPLIED.value,
                    )
                ),
                ~Opportunity.archive_record.has(
                    OpportunityArchive.archived_at.is_not(None)
                ),
            )
        ).all()
        for state, deadline, role_title, programme_group in rows:
            if out_of_scope_programme_reason(role_title, programme_group) is not None:
                continue
            if state not in _YII_SUPPRESSING_STATES:
                continue
            if deadline is not None and deadline < date.today():
                continue
            if state in _YII_SUPPRESSING_STATES:
                return True
        return False

    def autopilot_candidates(self) -> list[tuple[Application, Opportunity]]:
        """Applications the autopilot may drive right now, priority-ordered."""
        rows = self.session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(
                Application.state.in_(OPEN_STATES),
                Opportunity.application_window_status
                == ApplicationWindowStatus.OPEN.value,
                Opportunity.user_status.in_(
                    (
                        UserApplicationStatus.INTERESTED.value,
                        UserApplicationStatus.NOT_APPLIED.value,
                    )
                ),
                ~Opportunity.archive_record.has(
                    OpportunityArchive.archived_at.is_not(None)
                ),
            )
        ).all()
        candidates: list[tuple[Application, Opportunity]] = []
        for application, opportunity in rows:
            if out_of_scope_programme_reason(
                opportunity.role_title,
                opportunity.programme_group,
            ) is not None:
                continue
            if opportunity.automation_url is None:
                # Source/listing URLs remain visible but are never automation
                # destinations until target evidence is promoted.
                continue
            if opportunity.deadline is not None and opportunity.deadline < date.today():
                # Expired listings remain visible for audit/history, but never
                # enter the automated queue.
                continue
            group = opportunity.programme_group or classify_programme(
                opportunity.role_title
            ).value
            if not should_auto_apply(group):
                continue
            if group == ProgrammeType.SUMMER.value and self._employer_has_open_yii(
                opportunity.employer
            ):
                # firm has a Year in Industry open: skip its Summer entirely
                # (Yii takes priority; Summer is the fallback only)
                continue
            candidates.append((application, opportunity))
        candidates.sort(
            key=lambda pair: (
                user_status_work_rank(pair[1].user_status),
                pair[0].priority,
                pair[1].deadline or date.max,
            )
        )
        return candidates

    def _visible_login_wall_skips(
        self,
        *,
        exclude_application_ids: frozenset[str],
        scope_ids: frozenset[str] | None = None,
    ) -> list[tuple[Application, Opportunity]]:
        """Known login/captcha walls excluded from runnable candidates.

        Mirrors autopilot_candidates() filters exactly, replacing only the
        automation-url gate with the AUTH_WALL/HUMAN_CHALLENGE predicate, so
        a correctly classified wall is reported in skip counts/details while
        never entering runnable candidates, opening a browser, submitting,
        or consuming the runnable budget. Opportunity.automation_url and
        target trust are unchanged.
        """
        rows = self.session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(
                Application.state.in_(OPEN_STATES),
                Opportunity.application_window_status
                == ApplicationWindowStatus.OPEN.value,
                Opportunity.user_status.in_(
                    (
                        UserApplicationStatus.INTERESTED.value,
                        UserApplicationStatus.NOT_APPLIED.value,
                    )
                ),
                ~Opportunity.archive_record.has(
                    OpportunityArchive.archived_at.is_not(None)
                ),
                Opportunity.target_status.in_(_LOGIN_WALL_SKIP_KINDS),
            )
        ).all()
        skipped: list[tuple[Application, Opportunity]] = []
        for application, opportunity in rows:
            if application.id in exclude_application_ids:
                continue
            if scope_ids is not None and application.id not in scope_ids:
                continue
            if out_of_scope_programme_reason(
                opportunity.role_title,
                opportunity.programme_group,
            ) is not None:
                continue
            if opportunity.deadline is not None and opportunity.deadline < date.today():
                continue
            group = opportunity.programme_group or classify_programme(
                opportunity.role_title
            ).value
            if not should_auto_apply(group):
                continue
            if group == ProgrammeType.SUMMER.value and self._employer_has_open_yii(
                opportunity.employer
            ):
                continue
            skipped.append((application, opportunity))
        skipped.sort(
            key=lambda pair: (
                user_status_work_rank(pair[1].user_status),
                pair[0].priority,
                pair[1].deadline or date.max,
            )
        )
        return skipped

    def run_autopilot(
        self,
        runner_factory,
        *,
        max_runs: int = 10,
        headed: bool = False,
        submit: bool | None = None,
        confirmed_application_ids: Iterable[str] | None = None,
        application_scope: Iterable[str] | None = None,
    ) -> dict[str, object]:
        """Drive eligible applications through the full pipeline.

        runner_factory: callable(application_id, mode, headed) -> outcome dict
        (the API layer supplies the real AutomationRunner call). The autopilot
        runs REVIEW first. A Risk-0 outcome can proceed to SUBMIT only when
        live mode is armed *and* the exact application id is present in
        ``confirmed_application_ids``. The caller must populate that set only
        after a fresh, action-time confirmation of the displayed employer and
        role. Batch/scheduled callers intentionally omit it and therefore stop
        at the confirmation boundary. Anything else requiring a human stops
        cleanly and is reported. ``max_runs`` budgets runner REVIEW attempts;
        rows examined without a run are still counted in ``processed``.
        ``application_scope`` restricts the run to exact application IDs:
        ``None`` is the ordinary unscoped sweep (all candidates), while an
        explicit set — including the empty set — matches only those IDs and
        never falls back to all candidates. Batch confirmation supplies no
        session_id/authority_id, so a scoped run never submits: confirmed
        ready outcomes stop at an explicit action-time confirmation
        requirement pointing at the per-application submit route.
        """
        from app.automation.types import RunMode  # local import avoids cycle

        allow_submission = self.settings.submission_armed and (
            True if submit is None else bool(submit)
        )
        confirmed_ids = frozenset(str(item) for item in (confirmed_application_ids or ()))
        # None = autonomous sweep over every candidate. Any explicit value —
        # even empty — is a closed scope: only those application IDs may be
        # examined, run, or reported. There is no fallback to all candidates.
        scope_ids = (
            None
            if application_scope is None
            else frozenset(str(item) for item in application_scope)
        )
        results = {
            "processed": 0,
            "attempted_runs": 0,
            "submitted": 0,
            "needs_user": 0,
            "blocked": 0,
            "failed": 0,
            "skipped_login_required": 0,
            "mode": self.settings.automation_mode.value,
            "submission_armed": allow_submission,
            "details": [],
        }
        if not self.settings.autopilot_enabled:
            results["disabled_reason"] = "automation_off"
            self._audit_autopilot(results)
            return results
        # max_runs budgets runner attempts (REVIEW calls), not examinations:
        # rows that stop in the pipeline without a run (stale user status,
        # parked states, skips) are counted honestly in processed/blocked/
        # needs_user but must not starve a genuinely runnable no-login target
        # sorted behind them. Skipped login walls never consume this budget.
        seen_application_ids: set[str] = set()
        for application, opportunity in self.autopilot_candidates():
            if results["attempted_runs"] >= max_runs:
                break
            if scope_ids is not None and application.id not in scope_ids:
                # Out-of-scope rows are not examined: no pipeline mutation,
                # no counters, no budget consumption, no reporting here.
                continue
            seen_application_ids.add(application.id)
            if opportunity.is_archived:
                continue
            if not opportunity.is_open_for_applications:
                continue
            if out_of_scope_programme_reason(
                opportunity.role_title,
                opportunity.programme_group,
            ) is not None:
                continue
            entry: dict[str, object] = {
                "application_id": application.id,
                "employer": opportunity.employer,
                "role": opportunity.role_title,
                "programme": opportunity.programme_group,
            }
            if self.settings.autopilot_skip_login_required:
                try:
                    resolved_kind = (
                        TargetKind(opportunity.target_status)
                        if opportunity.target_status
                        else None
                    )
                except ValueError:
                    resolved_kind = None
                if resolved_kind in {
                    TargetKind.AUTH_WALL,
                    TargetKind.HUMAN_CHALLENGE,
                }:
                    entry["result"] = "skipped:login_required"
                    entry["reason"] = (
                        f"Target requires human login/verification "
                        f"({resolved_kind.value}); skipped by "
                        "ARGUS_AUTOPILOT_SKIP_LOGIN_REQUIRED"
                    )
                    results["skipped_login_required"] += 1
                    results["details"].append(entry)
                    continue
            self.session.flush()
            self.session.refresh(opportunity, attribute_names=["user_status"])
            if not user_status_is_automation_eligible(opportunity.user_status):
                entry["result"] = "blocked:user_status"
                entry["reason"] = (
                    f"User status {opportunity.user_status} excludes automation"
                )
                results["blocked"] += 1
                results["processed"] += 1
                results["details"].append(entry)
                continue
            try:
                service = ApplicationService(self.session, self.settings, self.crypto)
                if application.state == ApplicationState.DISCOVERED.value:
                    service.evaluate(opportunity.id)
                    self.session.refresh(application)
                if application.state == ApplicationState.DISCOVERED.value:
                    # evaluate() creates its own application row for the
                    # opportunity - re-point at whatever it returned instead of
                    # assuming this row moved.
                    fresh = self.session.execute(
                        select(Application).where(
                            Application.opportunity_id == opportunity.id
                        )
                    ).scalars().all()
                    if fresh:
                        application = max(
                            fresh,
                            key=lambda a: a.state != ApplicationState.DISCOVERED.value,
                        )
                        if scope_ids is not None and application.id not in scope_ids:
                            # evaluate() re-pointed at a different application
                            # row that is outside the confirmed scope. Fail
                            # closed: never widen the scope by replacement.
                            seen_application_ids.add(application.id)
                            entry["result"] = "blocked:scope_identity_changed"
                            entry["reason"] = (
                                "Pipeline re-pointed at an application outside "
                                "the confirmed scope; refusing to run it"
                            )
                            results["blocked"] += 1
                            results["processed"] += 1
                            results["details"].append(entry)
                            continue
                if application.state == ApplicationState.ELIGIBILITY_CHECKED.value:
                    service.queue(application.id)
                    service.prepare(application.id)
                    self.session.refresh(application)
                if application.state == ApplicationState.QUEUED.value:
                    service.prepare(application.id)
                    self.session.refresh(application)
                if application.state not in {
                    ApplicationState.PACKAGE_PREPARED.value,
                    ApplicationState.FILLING.value,
                    ApplicationState.FAILED_RETRYABLE.value,
                }:
                    if (
                        scope_ids is not None
                        and application.id in confirmed_ids
                        and application.state == ApplicationState.READY_TO_SUBMIT.value
                    ):
                        # A confirmed READY selection still carries no
                        # session_id/authority_id, so it cannot submit through
                        # the batch path. Keep it visible for the human with an
                        # explicit per-application action-time requirement.
                        entry["result"] = "awaiting_action_time_confirmation"
                        entry["confirmation_required"] = True
                        entry["state"] = ApplicationState.READY_TO_SUBMIT.value
                        entry["submit_route"] = (
                            f"/api/applications/{application.id}/run?mode=submit"
                        )
                        entry["reason"] = (
                            "Batch confirmation carries application IDs only; "
                            "final submission additionally requires session_id "
                            "and authority_id via POST "
                            "/api/applications/{id}/run?mode=submit"
                        )
                        results["needs_user"] += 1
                        results["details"].append(entry)
                        results["processed"] += 1
                        continue
                    entry["result"] = f"stopped:{application.state}"
                    if application.state in {
                        ApplicationState.NEEDS_USER.value,
                        ApplicationState.NEEDS_OA.value,
                    }:
                        results["needs_user"] += 1
                    else:
                        results["blocked"] += 1
                    results["details"].append(entry)
                    results["processed"] += 1
                    continue

                # Commit pipeline state changes so the runner's separate
                # session observes PACKAGE_PREPARED (SQLite isolation).
                self.session.commit()

                self.session.refresh(opportunity, attribute_names=["user_status"])
                if not user_status_is_automation_eligible(opportunity.user_status):
                    entry["result"] = "blocked:user_status"
                    entry["reason"] = (
                        f"User status {opportunity.user_status} excludes automation"
                    )
                    results["blocked"] += 1
                    results["processed"] += 1
                    results["details"].append(entry)
                    continue

                # First pass: review (fills the form, computes risk, no submit)
                results["attempted_runs"] += 1
                outcome = runner_factory(application.id, RunMode.REVIEW, headed)
                entry["risk"] = outcome.get("risk_level")
                entry["adapter"] = outcome.get("adapter")
                state = outcome.get("state")
                self.session.expire_all()
                application = self.session.get(Application, application.id)

                if outcome.get("risk_level") == 0 and state in {
                    "READY_TO_SUBMIT",
                    "PACKAGE_PREPARED",
                }:
                    if not allow_submission:
                        entry["result"] = "review_only"
                        entry["state"] = state
                        results["processed"] += 1
                        results["details"].append(entry)
                        continue
                    if not self._conflict_rules_configured(opportunity):
                        entry["result"] = "blocked:conflict_rules_not_configured"
                        entry["blocked_reasons"] = ["conflict_rules_not_configured"]
                        results["blocked"] += 1
                        results["processed"] += 1
                        results["details"].append(entry)
                        continue
                    if application.id not in confirmed_ids:
                        entry["result"] = "awaiting_action_time_confirmation"
                        entry["confirmation_required"] = True
                        entry["state"] = state
                        results["needs_user"] += 1
                        results["processed"] += 1
                        results["details"].append(entry)
                        continue
                    if scope_ids is not None:
                        # Confirmed application IDs are not submit authority:
                        # the batch path never supplies session_id and
                        # authority_id, so the runner guards could not accept
                        # a submit. Stop here with an explicit per-application
                        # action-time requirement instead of calling SUBMIT.
                        entry["result"] = "awaiting_action_time_confirmation"
                        entry["confirmation_required"] = True
                        entry["state"] = state
                        entry["submit_route"] = (
                            f"/api/applications/{application.id}/run?mode=submit"
                        )
                        entry["reason"] = (
                            "Batch confirmation carries application IDs only; "
                            "final submission additionally requires session_id "
                            "and authority_id via POST "
                            "/api/applications/{id}/run?mode=submit"
                        )
                        results["needs_user"] += 1
                        results["processed"] += 1
                        results["details"].append(entry)
                        continue
                    opportunity = application.opportunity
                    self.session.refresh(
                        opportunity,
                        attribute_names=["user_status"],
                    )
                    if not user_status_is_automation_eligible(
                        opportunity.user_status
                    ):
                        entry["result"] = "blocked:user_status"
                        entry["reason"] = (
                            f"User status {opportunity.user_status} excludes submission"
                        )
                        results["blocked"] += 1
                        results["processed"] += 1
                        results["details"].append(entry)
                        continue
                    submit_outcome = runner_factory(application.id, RunMode.SUBMIT, headed)
                    self.session.expire_all()
                    application = self.session.get(Application, application.id)
                    receipt_reference = self._persisted_submission_reference(application)
                    entry["receipt"] = receipt_reference
                    entry["state"] = application.state if application else submit_outcome.get("state")
                    if receipt_reference and application and application.state in _SUBMISSION_TERMINAL_STATES:
                        entry["result"] = "submitted"
                        results["submitted"] += 1
                    else:
                        entry["result"] = "submit_unverified"
                        entry["blocked_reasons"] = ["submission_not_persisted"]
                        results["blocked"] += 1
                elif state in {"NEEDS_USER", "NEEDS_OA"} or outcome.get("blocked_reasons"):
                    entry["result"] = "needs_user"
                    entry["blocked_reasons"] = outcome.get("blocked_reasons")
                    results["needs_user"] += 1
                else:
                    entry["result"] = f"review:{state}"
                    results["blocked"] += 1
            except ApplicationBlockedError as exc:
                entry["result"] = "blocked"
                entry["reason"] = str(exc)
                results["blocked"] += 1
                self.session.rollback()
            except Exception as exc:  # noqa: BLE001
                logger.exception("autopilot run failed for %s", application.id)
                entry["result"] = "failed"
                entry["reason"] = str(exc)[:300]
                results["failed"] += 1
                # a runner-side DB conflict (e.g. database-is-locked) poisons the
                # session's transaction; roll back so later runs still work
                self.session.rollback()
                application = self.session.get(Application, entry["application_id"])
                if application is None:
                    continue
            results["processed"] += 1
            results["details"].append(entry)
        if self.settings.autopilot_skip_login_required:
            # Correctly classified walls never enter runnable candidates, so
            # without this pass they are invisible in sweep accounting. Report
            # them here: details/skip counts only, no runner, no submission,
            # no runnable-budget consumption. Rows already reported in the loop
            # above (stale or mocked) are excluded to avoid double counting.
            for application, opportunity in self._visible_login_wall_skips(
                exclude_application_ids=frozenset(seen_application_ids),
                scope_ids=scope_ids,
            ):
                seen_application_ids.add(application.id)
                try:
                    wall_kind = (
                        TargetKind(opportunity.target_status)
                        if opportunity.target_status
                        else None
                    )
                except ValueError:
                    wall_kind = None
                if wall_kind not in {
                    TargetKind.AUTH_WALL,
                    TargetKind.HUMAN_CHALLENGE,
                }:
                    continue
                results["details"].append(
                    {
                        "application_id": application.id,
                        "employer": opportunity.employer,
                        "role": opportunity.role_title,
                        "programme": opportunity.programme_group,
                        "result": "skipped:login_required",
                        "reason": (
                            "Target requires human login/verification "
                            f"({wall_kind.value}); skipped by "
                            "ARGUS_AUTOPILOT_SKIP_LOGIN_REQUIRED"
                        ),
                    }
                )
                results["skipped_login_required"] += 1
        if scope_ids is not None:
            # Candidate selection and the run budget say nothing about existence.
            # Report unvisited scoped rows without making them runnable.
            for pending_id in sorted(scope_ids - seen_application_ids):
                known = self.session.get(Application, pending_id)
                if known is None:
                    results["details"].append({
                        "application_id": pending_id,
                        "result": "unknown_application_id",
                    })
                    continue
                opportunity = self.session.get(Opportunity, known.opportunity_id)
                detail = {"application_id": pending_id, "state": known.state}
                if (
                    self.settings.autopilot_skip_login_required
                    and opportunity is not None
                    and opportunity.target_status in {
                        TargetKind.AUTH_WALL.value, TargetKind.HUMAN_CHALLENGE.value,
                    }
                ):
                    detail["result"] = "skipped:login_required"
                    results["skipped_login_required"] += 1
                elif known.state in {
                    ApplicationState.NEEDS_USER.value, ApplicationState.NEEDS_OA.value,
                }:
                    detail["result"] = f"stopped:{known.state}"
                    results["needs_user"] += 1
                    results["processed"] += 1
                elif known.state in OPEN_STATES and results["attempted_runs"] >= max_runs:
                    detail["result"] = "deferred:max_runs"
                else:
                    detail["result"] = f"stopped:{known.state}"
                    detail["reason"] = "not_eligible_for_autopilot"
                results["details"].append(detail)
        self.session.flush()
        self._audit_autopilot(results)
        return results

    def _conflict_rules_configured(self, opportunity: Opportunity) -> bool:
        """Require a rule that actually covers this employer and cycle."""

        for rule in self.session.scalars(select(ConflictRule)).all():
            if rule.cycle.casefold() != opportunity.cycle.casefold():
                continue
            if fnmatchcase(opportunity.employer.casefold(), rule.employer_pattern.casefold()):
                return True
        return False

    def _persisted_submission_reference(self, application: Application | None) -> str:
        """Read submission evidence committed by the runner's own session."""

        if application is None or application.state not in _SUBMISSION_TERMINAL_STATES:
            return ""
        if application.submission_reference.strip():
            return application.submission_reference.strip()
        latest = self.session.execute(
            select(AutomationRun)
            .where(
                AutomationRun.application_id == application.id,
                AutomationRun.mode == "submit",
            )
            .order_by(AutomationRun.created_at.desc())
        ).scalars().first()
        if latest is None:
            return ""
        try:
            receipt = json.loads(latest.receipt_json or "{}")
        except (TypeError, ValueError):
            return ""
        return str(receipt.get("reference") or "").strip()

    def _audit_autopilot(self, results: dict[str, object]) -> None:
        append_audit(
            self.session,
            AuditInput(
                "scout",
                "scout.autopilot_run",
                "system",
                "autopilot",
                {
                    k: v
                    for k, v in results.items()
                    if k != "details"
                },
            ),
        )
