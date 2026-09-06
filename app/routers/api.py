from __future__ import annotations

import hashlib
import inspect
import ipaddress
import json
import re
import secrets
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import delete, select

from app.automation.host_policy import (
    normalise_hostname,
    origin_for_url,
    safe_public_navigation_url,
)
from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.automation.types import RunMode
from app.automation.targets import (
    EMPLOYER_EVIDENCE_KEYS,
    ROLE_EVIDENCE_KEYS,
    TargetResolution,
    normalise_evidence_key,
)
from app.domain.states import (
    ApplicationState,
    UserApplicationStatus,
    user_status_exclusion_reason,
    user_status_is_automation_eligible,
)
from app.domain.targets import TargetKind, validate_navigation_url
from app.models import (
    AnswerEntry,
    Application,
    AuditEvent,
    AutomationRun,
    Document,
    ConflictRule,
    LabSubmission,
    Opportunity,
    QuestionRecord,
)
from app.routers.deps import SessionDep
from app.security.audit import AuditInput, append_audit, verify_audit_chain
from app.services.answers import AnswerService
from app.services.applications import ApplicationBlockedError, ApplicationService
from app.services.dashboard import DashboardService
from app.services.documents import DocumentService
from app.services.opportunities import (
    OpportunityService,
    normalise_url,
)
from app.services.profile import ProfileService, ProfileUpdate
from app.services.target_resolution import (
    NavigatorTargetResolver,
    ResolutionOutcome,
    TargetResolutionService,
    UserStatusAutomationExcludedError,
)
from app.services.navigator import DuplicateSessionError, SourceResolutionCapability
from app.services.prefill import (
    HUMAN_BOUNDARY_COPY,
    PREFILL_APPLICATION_STATES,
    PREFILL_TARGET_STATUS,
    PrefillBlocked,
    prefill_start_reason,
    prefill_wall,
    row_application_url_allowlist,
    sanitise_blocked_requests,
)
from app.services.user_statuses import UserStatusChange, set_user_status

router = APIRouter(prefix="/api", tags=["api"])

# Candidate double-clicks are serialized per application.  The runner's
# independent claim guard remains authoritative for every caller, including
# CLI/scheduler paths; this lock only makes the HTTP start affordance honest.
_PREFILL_LOCKS: dict[str, threading.Lock] = {}
_PREFILL_LOCKS_GUARD = threading.Lock()


def _prefill_lock(application_id: str) -> threading.Lock:
    with _PREFILL_LOCKS_GUARD:
        return _PREFILL_LOCKS.setdefault(application_id, threading.Lock())


def _prefill_continue_payload(snapshot: object | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    state = getattr(snapshot, "state", "")
    state_value = getattr(state, "value", state)
    boundary = getattr(snapshot, "human_boundary", {}) or {}
    return {
        "session_id": str(getattr(snapshot, "session_id", "")),
        "application_id": str(getattr(snapshot, "application_id", "")),
        "mode": str(getattr(snapshot, "mode", RunMode.PREFILL.value)),
        "state": str(state_value),
        "status": str(getattr(snapshot, "status", state_value)),
        "reason": str(getattr(snapshot, "reason", "")),
        "headed": bool(getattr(snapshot, "headed", True)),
        "human_boundary": dict(boundary) if isinstance(boundary, Mapping) else {},
        "can_continue": bool(
            boundary.get("can_continue", True)
            if isinstance(boundary, Mapping)
            else True
        ),
        "can_cancel": bool(
            boundary.get("can_cancel", True)
            if isinstance(boundary, Mapping)
            else True
        ),
    }


# The candidate reads ``wall`` in the Needs You queue when a start is refused.
#
# Most refusal texts already name the specific thing that stopped the run --
# which status excluded the row, which target state it actually has -- and that
# detail is the actionable part.  Those are kept verbatim and a next step is
# appended.  Only refusals phrased purely for a maintainer ("Serialized
# target-resolution envelope disagrees with authoritative target columns") are
# replaced outright, because they name nothing a candidate can act on.
# ``message`` always retains the exact technical text for diagnosis.
_PREFILL_WALL_REPLACE: dict[str, str] = {
    "target_evidence_invalid": (
        "ARGUS cannot confirm this row still points at the application form it "
        "verified earlier, so it refused to open a browser. The stored source "
        "link and the recorded evidence disagree. Re-resolve this target, or "
        "supply the employer application URL yourself."
    ),
    "navigator_state_unavailable": (
        "ARGUS could not confirm whether a browser session is already open, so "
        "it started nothing. Try again in a moment."
    ),
}

_PREFILL_WALL_NEXT_STEP: dict[str, str] = {
    "user_status_excluded": "Change the status if you want ARGUS to prepare it.",
    "target_not_application_form": (
        "Resolve the target first; there is no form to fill yet."
    ),
    "application_state_not_prepareable": (
        "Check the recorded blockers for this application below."
    ),
    "prefill_session_active": "Use Continue rather than starting a second run.",
    "prefill_application_url_missing": (
        "Supply the employer application URL yourself to continue."
    ),
    "prefill_application_url_invalid": (
        "Supply a corrected employer application URL to continue."
    ),
}


def _prefill_presented_wall(code: str, wall: str) -> str:
    """Keep the specific reason, add a next step, hide maintainer-only text."""

    replacement = _PREFILL_WALL_REPLACE.get(code)
    if replacement is not None:
        return replacement
    next_step = _PREFILL_WALL_NEXT_STEP.get(code)
    if not next_step:
        return wall
    detail = wall.strip()
    if not detail:
        return next_step
    separator = " " if detail.endswith((".", "!", "?")) else ". "
    return f"{detail}{separator}{next_step}"


def _prefill_error(
    application_id: str,
    code: str,
    wall: str,
    *,
    continue_payload: dict[str, object] | None = None,
) -> JSONResponse:
    content: dict[str, object] = {
        "application_id": application_id,
        "mode": RunMode.PREFILL.value,
        "headed": True,
        "submitted": False,
        "click_boundary_crossed": False,
        "code": code,
        "message": wall,
        "wall": _prefill_presented_wall(code, wall),
    }
    if continue_payload is not None:
        content["continue"] = continue_payload
    return JSONResponse(content, status_code=409)


def _prefill_run_wall(
    request: Request,
    application_id: str,
    run_id: str,
) -> tuple[str, tuple[dict[str, str], ...]]:
    """Read only sanitized failure evidence from the completed run audit."""

    if not run_id:
        return "", ()
    with request.app.state.db.session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None or str(run.application_id) != str(application_id):
            return "", ()
        event = session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "automation_run",
                AuditEvent.entity_id == run.id,
                AuditEvent.event_type == "automation.run_finished",
            )
            .order_by(AuditEvent.id.desc())
        )
        details: Mapping[str, object] = {}
        if event is not None:
            try:
                parsed = json.loads(event.details_json or "{}")
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping):
                details = parsed
        blocked = sanitise_blocked_requests(details.get("blocked_requests", ()))
        return prefill_wall(
            run_error=run.error,
            blocked_requests=blocked,
            state=run.state,
        ), blocked


def _prefill_outcome_payload(
    request: Request,
    application_id: str,
    outcome: object,
) -> dict[str, object]:
    run_id = str(getattr(outcome, "run_id", "") or "")
    run_wall, blocked = _prefill_run_wall(request, application_id, run_id)
    session_id = str(getattr(outcome, "handoff_session_id", "") or "")
    state = str(getattr(outcome, "state", "") or "")
    human_boundary_value = getattr(outcome, "human_boundary", {}) or {}
    human_boundary = (
        dict(human_boundary_value)
        if isinstance(human_boundary_value, Mapping)
        else {}
    )
    active = None
    navigator = getattr(request.app.state, "navigator", None)
    if session_id and navigator is not None:
        try:
            candidate = navigator.active_for_application(application_id)
        except Exception:  # noqa: BLE001 - response must remain fail-closed
            candidate = None
        if candidate is not None and str(getattr(candidate, "session_id", "")) == session_id:
            active = candidate
    # A handoff envelope without a live session is not actionable. Prefer the
    # recorded run wall in that case so egress failures remain visible.
    wall = HUMAN_BOUNDARY_COPY if (session_id or active is not None) else run_wall
    if not wall:
        wall = prefill_wall(state=state)
    receipt = getattr(outcome, "receipt", None)
    return {
        "application_id": application_id,
        "mode": RunMode.PREFILL.value,
        "headed": True,
        "run_id": run_id,
        "state": state,
        "risk_level": int(getattr(outcome, "risk_level", 4) or 0),
        "adapter": str(getattr(outcome, "adapter", "") or ""),
        "blocked_reasons": tuple(getattr(outcome, "blocked_reasons", ()) or ()),
        "receipt": (
            {
                "reference": receipt.reference,
                "url": receipt.url,
                "confirmation_text": receipt.confirmation_text,
            }
            if receipt is not None
            else None
        ),
        "trace_path": str(getattr(outcome, "trace_path", "") or ""),
        "screenshot_path": str(getattr(outcome, "screenshot_path", "") or ""),
        "session_id": session_id,
        "handoff_session_id": session_id,
        "human_boundary": human_boundary,
        "blocked_egress": list(blocked),
        "wall": wall,
        "message": wall,
        "submitted": False,
        "click_boundary_crossed": False,
        **({"continue": _prefill_continue_payload(active)} if active is not None else {}),
    }


class ProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    preferred_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=80)
    address_line1: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, max_length=120)
    postcode: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=120)
    linkedin_url: str | None = Field(default=None, max_length=500)
    university: str | None = Field(default=None, max_length=240)
    degree: str | None = Field(default=None, max_length=240)
    graduation_year: int | None = Field(default=None, ge=2000, le=2100)
    current_study_year: str | None = Field(default=None, max_length=80)
    preferred_locations: tuple[str, ...] | None = None
    work_authorisation: str | None = Field(default=None, max_length=1000)
    requires_sponsorship: bool | None = None
    work_authorisation_approved: bool | None = None


class AnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_key: str = Field(min_length=2, max_length=160)
    prompt: str = Field(default="", max_length=4000)
    answer: str = Field(min_length=1, max_length=20000)
    approved: bool = False
    sensitive: bool = False
    category: str = Field(default="general", max_length=80)
    evidence: str = Field(default="", max_length=4000)
    max_characters: int | None = Field(default=None, ge=1, le=50000)


class ConflictRulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employer_pattern: str = Field(min_length=1, max_length=240)
    cycle: str = Field(min_length=1, max_length=40)
    max_applications: int | None = Field(default=None, ge=1, le=100)
    exclusive_groups: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=4000)


class OpportunityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employer: str = Field(min_length=1, max_length=240)
    role_title: str = Field(min_length=1, max_length=320)
    division: str = Field(default="", max_length=240)
    programme_group: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=240)
    cycle: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=8, max_length=4000)
    source: str = Field(default="manual", max_length=120)
    ats_type: str = Field(default="unknown", max_length=80)
    deadline: date | None = None
    rolling: bool = False
    min_graduation_year: int | None = Field(default=None, ge=2000, le=2100)
    max_graduation_year: int | None = Field(default=None, ge=2000, le=2100)
    sponsorship_supported: bool | None = None
    cv_required: bool = True
    cover_letter_required: bool = False
    written_answers_required: bool = False
    notes: str = Field(default="", max_length=10000)


class CapturePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    employer: str = Field(min_length=1, max_length=240)
    role_title: str = Field(min_length=1, max_length=320)
    cycle: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=8, max_length=4000)
    division: str = Field(default="", max_length=240)
    location: str = Field(default="", max_length=240)
    source: str = Field(default="browser_capture", max_length=120)


class RunApplicationBody(BaseModel):
    """Submit authority binding; all target material is server-derived."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)


class BlockerResolutionPayload(BaseModel):
    """Human-bound proof used to re-evaluate one blocked application.

    Resolving a blocker is deliberately separate from the generic queue
    endpoint.  The exact application id, an action-time confirmation, and a
    human reason are required; the server then re-runs its own checks and
    never starts an automation/submission run.
    """

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1)
    application_url: str | None = Field(default=None, max_length=2048)
    resolution_reason: str = Field(min_length=8, max_length=4000)
    confirmed: bool = False


class ListingChoicePayload(BaseModel):
    """Human choice of one URL already enumerated from a listing surface."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1)
    application_url: str = Field(min_length=8, max_length=2048)
    resolution_reason: str = Field(min_length=8, max_length=4000)
    confirmed: bool = False


class ResolveTargetPayload(BaseModel):
    """Optional exact-id binding for a user-triggered target-resolution action.

    No URL or destination field is accepted.  The path id and the stored
    opportunity/application relationship remain authoritative.
    """

    model_config = ConfigDict(extra="forbid")

    opportunity_id: str | None = Field(default=None, min_length=1)
    application_id: str | None = Field(default=None, min_length=1)
    confirmed: bool = False


class UserStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UserApplicationStatus


def _user_status(record: Opportunity, *, change: UserStatusChange | None = None) -> dict[str, object]:
    return {
        "opportunity_id": record.id,
        "status": record.user_status,
        "actor": record.user_status_actor,
        "updated_at": (
            record.user_status_updated_at.isoformat()
            if record.user_status_updated_at
            else None
        ),
        "automation_eligible": user_status_is_automation_eligible(record.user_status),
        "automation_exclusion_reason": user_status_exclusion_reason(
            record.user_status
        ),
        "changed": change.changed if change is not None else False,
    }


def _opportunity(record: Opportunity) -> dict[str, object]:
    try:
        resolution_evidence = json.loads(record.resolution_evidence_json or "{}")
    except (TypeError, ValueError):
        resolution_evidence = {}
    try:
        navigation_url = record.navigation_url
    except ValueError:
        navigation_url = None
    return {
        "id": record.id,
        "employer": record.employer,
        "role_title": record.role_title,
        "division": record.division,
        "programme_group": record.programme_group,
        "location": record.location,
        "cycle": record.cycle,
        "url": record.url,
        "source_url": record.url,
        "navigation_url": navigation_url,
        "application_url": record.application_url,
        "automation_url": record.automation_url,
        "source_fingerprint": record.source_fingerprint,
        "target_status": record.target_status,
        "resolved_ats_type": record.resolved_ats_type,
        "user_status": record.user_status,
        "user_status_actor": record.user_status_actor,
        "user_status_updated_at": (
            record.user_status_updated_at.isoformat()
            if record.user_status_updated_at
            else None
        ),
        "automation_eligible_by_user_status": user_status_is_automation_eligible(
            record.user_status
        ),
        "automation_exclusion_reason": user_status_exclusion_reason(
            record.user_status
        ),
        "resolution_evidence": resolution_evidence,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        "resolution_attempted_at": (
            record.resolution_attempted_at.isoformat()
            if record.resolution_attempted_at
            else None
        ),
        "source": record.source,
        "ats_type": record.ats_type,
        "deadline": record.deadline.isoformat() if record.deadline else None,
        "rolling": record.rolling,
        "min_graduation_year": record.min_graduation_year,
        "max_graduation_year": record.max_graduation_year,
        "sponsorship_supported": record.sponsorship_supported,
        "cv_required": record.cv_required,
        "cover_letter_required": record.cover_letter_required,
        "written_answers_required": record.written_answers_required,
        "notes": record.notes,
    }


def _document(record: Document) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "kind": record.kind,
        "sha256": record.sha256,
        "approved": record.approved,
        "tags": json.loads(record.tags_json or "[]"),
        "created_at": record.created_at.isoformat(),
    }


def _application(record: Application) -> dict[str, object]:
    opportunity = record.opportunity
    return {
        "id": record.id,
        "opportunity_id": record.opportunity_id,
        "employer": opportunity.employer,
        "role_title": opportunity.role_title,
        "division": opportunity.division,
        "location": opportunity.location,
        "cycle": opportunity.cycle,
        "url": opportunity.url,
        "source_url": opportunity.url,
        "source_fingerprint": opportunity.source_fingerprint,
        "application_url": opportunity.application_url,
        "automation_url": opportunity.automation_url,
        "target_status": opportunity.target_status,
        "resolved_ats_type": opportunity.resolved_ats_type,
        "user_status": opportunity.user_status,
        "user_status_actor": opportunity.user_status_actor,
        "user_status_updated_at": (
            opportunity.user_status_updated_at.isoformat()
            if opportunity.user_status_updated_at
            else None
        ),
        "automation_eligible_by_user_status": user_status_is_automation_eligible(
            opportunity.user_status
        ),
        "automation_exclusion_reason": user_status_exclusion_reason(
            opportunity.user_status
        ),
        "state": record.state,
        "priority": record.priority,
        "risk_level": record.risk_level,
        "next_action": record.next_action,
        "next_action_deadline": (
            record.next_action_deadline.isoformat() if record.next_action_deadline else None
        ),
        "submission_reference": record.submission_reference,
        "selected_cv_id": record.selected_cv_id,
        "selected_cover_letter_id": record.selected_cover_letter_id,
    }


@router.get("/dashboard")
def dashboard(session: SessionDep) -> dict[str, object]:
    snapshot = DashboardService(session).snapshot()
    return {
        "total_applications": snapshot.total_applications,
        "total_opportunities": snapshot.total_opportunities,
        "needs_user": snapshot.needs_user,
        "oa_pending": snapshot.oa_pending,
        "submitted": snapshot.submitted,
        "pipeline": snapshot.pipeline,
        "urgent_actions": [
            {
                "application_id": item.application_id,
                "employer": item.employer,
                "role_title": item.role_title,
                "action": item.action,
                "deadline": item.deadline.isoformat(),
                "state": item.state,
            }
            for item in snapshot.urgent_actions
        ],
    }


@router.get("/dashboard/funnel")
def dashboard_funnel(session: SessionDep) -> dict[str, object]:
    """Per-firm pipeline telemetry: scraped -> eligible -> queued -> submitted."""
    from app.services.funnel import FunnelService

    service = FunnelService(session)
    return {
        "totals": service.totals(),
        "firms": [
            {
                "employer": item.employer,
                "discovered": item.discovered,
                "eligible": item.eligible,
                "queued": item.queued,
                "submitted": item.submitted,
                "needs_user": item.needs_user,
                "blocked": item.blocked,
                "oa_pending": item.oa_pending,
                "open_roles": item.open_roles,
                "next_deadline": (
                    item.next_deadline.isoformat() if item.next_deadline else None
                ),
                "conversion": item.conversion,
            }
            for item in service.per_firm()
        ],
    }


@router.get("/profile")
def get_profile(request: Request, session: SessionDep) -> dict[str, object]:
    return ProfileService(session, request.app.state.crypto).public_dict()


@router.put("/profile")
def put_profile(
    payload: ProfilePayload, request: Request, session: SessionDep
) -> dict[str, object]:
    service = ProfileService(session, request.app.state.crypto)
    service.update(ProfileUpdate(**payload.model_dump()))
    return service.public_dict()


@router.get("/answers")
def list_answers(request: Request, session: SessionDep) -> list[dict[str, object]]:
    return AnswerService(session, request.app.state.crypto).list_public()


@router.post("/answers", status_code=201)
def upsert_answer(
    payload: AnswerPayload, request: Request, session: SessionDep
) -> dict[str, object]:
    service = AnswerService(session, request.app.state.crypto)
    entry = service.upsert(**payload.model_dump())
    return next(item for item in service.list_public() if item["id"] == entry.id)


@router.get("/documents")
def list_documents(session: SessionDep) -> list[dict[str, object]]:
    return [_document(item) for item in session.scalars(select(Document).order_by(Document.created_at.desc()))]


@router.post("/documents", status_code=201)
def upload_document(
    request: Request,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "cv",
    approved: Annotated[bool, Form()] = False,
    tags: Annotated[str, Form()] = "",
) -> dict[str, object]:
    content = file.file.read()
    if not content:
        raise HTTPException(400, "Document is empty")
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Document exceeds 10 MiB")
    record = DocumentService(session, request.app.state.settings.documents_dir).store_bytes(
        filename=file.filename or "document",
        content=content,
        kind=kind.strip() or "cv",
        approved=approved,
        tags=tuple(item.strip() for item in tags.split(",") if item.strip()),
    )
    return _document(record)


def _parse_exclusive_groups(value: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for raw_group in value.split(";"):
        members = sorted(
            {
                member.strip().casefold().replace(" ", "_")
                for member in raw_group.split(",")
                if member.strip()
            }
        )
        if not members:
            continue
        if len(members) < 2:
            raise HTTPException(422, "Each exclusive group must contain at least two names")
        groups.append(members)
    return groups


def _conflict_rule(record: ConflictRule) -> dict[str, object]:
    return {
        "id": record.id,
        "employer_pattern": record.employer_pattern,
        "cycle": record.cycle,
        "max_applications": record.max_applications,
        "exclusive_groups": json.loads(record.exclusive_groups_json or "[]"),
        "notes": record.notes,
    }


@router.get("/conflict-rules")
def list_conflict_rules(session: SessionDep) -> list[dict[str, object]]:
    records = list(
        session.scalars(
            select(ConflictRule).order_by(ConflictRule.employer_pattern, ConflictRule.cycle)
        ).all()
    )
    return [_conflict_rule(record) for record in records]


@router.post("/conflict-rules", status_code=201)
def create_conflict_rule(
    payload: ConflictRulePayload,
    session: SessionDep,
) -> dict[str, object]:
    groups = _parse_exclusive_groups(payload.exclusive_groups)
    employer_pattern = payload.employer_pattern.strip()
    cycle = payload.cycle.strip()
    duplicate = session.scalar(
        select(ConflictRule).where(
            ConflictRule.employer_pattern == employer_pattern,
            ConflictRule.cycle == cycle,
        )
    )
    if duplicate is not None:
        raise HTTPException(409, "A conflict rule already exists for this pattern and cycle")
    record = ConflictRule(
        employer_pattern=employer_pattern,
        cycle=cycle,
        max_applications=payload.max_applications,
        exclusive_groups_json=json.dumps(groups, sort_keys=True),
        notes=payload.notes.strip(),
    )
    session.add(record)
    session.flush()
    append_audit(
        session,
        AuditInput(
            "user",
            "conflict_rule.created",
            "conflict_rule",
            record.id,
            {
                "employer_pattern": employer_pattern,
                "cycle": cycle,
                "max_applications": payload.max_applications,
                "exclusive_groups": groups,
            },
        ),
    )
    return _conflict_rule(record)


@router.delete("/conflict-rules/{rule_id}", status_code=204)
def delete_conflict_rule(rule_id: str, session: SessionDep) -> Response:
    record = session.get(ConflictRule, rule_id)
    if record is None:
        raise HTTPException(404, "Conflict rule not found")
    details = {"employer_pattern": record.employer_pattern, "cycle": record.cycle}
    session.delete(record)
    session.flush()
    append_audit(
        session,
        AuditInput("user", "conflict_rule.deleted", "conflict_rule", rule_id, details),
    )
    return Response(status_code=204)


@router.get("/opportunities")
def list_opportunities(session: SessionDep) -> list[dict[str, object]]:
    return [_opportunity(item) for item in OpportunityService(session).list()]


@router.get("/opportunities/{opportunity_id}/user-status")
def get_opportunity_user_status(
    opportunity_id: str,
    session: SessionDep,
) -> dict[str, object]:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(404, "Opportunity not found")
    return _user_status(opportunity)


@router.put("/opportunities/{opportunity_id}/user-status")
def put_opportunity_user_status(
    opportunity_id: str,
    payload: UserStatusPayload,
    session: SessionDep,
) -> dict[str, object]:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(404, "Opportunity not found")
    change = set_user_status(
        session,
        opportunity,
        payload.status,
        actor="user",
    )
    return _user_status(opportunity, change=change)


@router.post("/opportunities", status_code=201)
def create_opportunity(payload: OpportunityPayload, session: SessionDep) -> dict[str, object]:
    service = OpportunityService(session)
    candidate = Opportunity(**payload.model_dump())
    try:
        existing = service.find_exact(candidate)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if existing:
        raise HTTPException(409, "Opportunity already exists")
    record = service.add(candidate)
    return _opportunity(record)


@router.post("/opportunities/import-csv")
def import_csv(
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    source: Annotated[str, Form()] = "csv",
) -> dict[str, object]:
    report = OpportunityService(session).import_csv(file.file.read(), source)
    return {
        "imported": report.imported,
        "skipped_duplicates": report.skipped_duplicates,
        "errors": [
            {"row_number": item.row_number, "message": item.message} for item in report.errors
        ],
    }


@router.post("/opportunities/{opportunity_id}/evaluate")
def evaluate_opportunity(
    opportunity_id: str, request: Request, session: SessionDep
) -> dict[str, object]:
    try:
        result = ApplicationService(
            session, request.app.state.settings, request.app.state.crypto
        ).evaluate(opportunity_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "application_id": result.application.id,
        "state": result.application.state,
        "eligible": result.eligible,
        "requires_review": result.requires_review,
        "conflict_blocked": result.conflict_blocked,
        "reason_codes": result.reason_codes,
    }


def _target_resolver(request: Request):
    """Return only the server-owned visible Navigator resolver.

    A resolver is deliberately obtained from application state.  The HTTP
    caller supplies an exact record id, never a destination URL.  The
    Navigator worker may return a typed ``TargetResolution`` or a human
    handoff snapshot; both are checked by ``TargetResolutionService``.
    """

    configured = getattr(request.app.state, "target_resolution_resolver", None)
    if configured is not None:
        return configured
    navigator = getattr(request.app.state, "navigator", None) or getattr(
        request.app.state, "handoff_manager", None
    )
    if navigator is None:
        return None
    # The adapter turns the headed Navigator's public method into the typed
    # service callback.  If the owner has not wired the method yet it raises
    # inside the service, which records an unresolved human handoff rather
    # than silently claiming a target.
    return NavigatorTargetResolver(navigator)


_MANUAL_APPLICATION_URL_MAX_LENGTH = 2048
_MANUAL_TARGET_FAILURE_KINDS = frozenset(
    {
        TargetKind.UNRESOLVED.value,
        TargetKind.MISSING_EMPLOYER_LINK.value,
        TargetKind.LISTING.value,
        TargetKind.MULTIPLE_CANDIDATE_ROLES.value,
        TargetKind.JOB_DETAIL.value,
        TargetKind.AUTH_WALL.value,
        TargetKind.HUMAN_CHALLENGE.value,
        TargetKind.NON_HTML.value,
        TargetKind.MISMATCH.value,
        TargetKind.BLOCKED.value,
    }
)
_MANUAL_ELIGIBLE_KINDS = frozenset(
    {TargetKind.APPLICATION_ENTRY.value, TargetKind.APPLICATION_FORM.value}
)
_MANUAL_IDENTITY_FAILURE_TERMS = (
    "target",
    "resolver",
    "destination",
    "application url",
    "application_url",
    "source inspection",
    "unverified",
)
_MANUAL_ELIGIBILITY_TERMS = (
    "eligibility",
    "conflict",
    "employer conflict",
    "missing answer",
)


def _manual_identity_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _manual_is_loopback_127(hostname: object) -> bool:
    return normalise_hostname(str(hostname or "")) == "127.0.0.1"


def _manual_target_lab_enabled(request: Request) -> bool:
    """Allow loopback URL fixtures only in an explicit, non-live test mode."""

    settings = getattr(request.app.state, "settings", None)
    if getattr(request.app.state, "manual_target_lab_enabled", None) is not True:
        return False
    if bool(getattr(settings, "live_submit_enabled", False)):
        return False
    if bool(getattr(settings, "live_submit_environment_enabled", False)):
        return False
    host = str(getattr(settings, "host", "") or "").casefold().strip()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_human_supplied_application_url(
    raw: str,
    *,
    allow_loopback: bool = False,
) -> str:
    """Validate and normalize a human-provided application URL fail-closed.

    Public URLs are HTTPS-only, credential-free, safe-public hosts on the
    default HTTPS port. The sole exception is an explicitly enabled local
    lab, where ``127.0.0.1`` may use a loopback HTTP fixture port. This helper
    never resolves or stores a caller URL as an automation target by itself;
    the shared typed target-resolution service remains authoritative.
    """

    if not isinstance(raw, str):
        raise ValueError("manual_application_url_must_be_text")
    value = raw.strip()
    if not value:
        raise ValueError("manual_application_url_required")
    if len(value) > _MANUAL_APPLICATION_URL_MAX_LENGTH:
        raise ValueError("manual_application_url_too_long")
    if any(
        ord(character) < 32 or ord(character) == 127 or character.isspace()
        for character in value
    ):
        raise ValueError("manual_application_url_control_or_whitespace")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("manual_application_url_malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("manual_application_url_credentials_forbidden")
    if not hostname:
        raise ValueError("manual_application_url_hostname_required")
    if any(ord(character) > 127 for character in hostname):
        raise ValueError("manual_application_url_unicode_host_forbidden")

    normalized_host = normalise_hostname(hostname)
    is_lab_loopback = _manual_is_loopback_127(normalized_host)
    if is_lab_loopback:
        if not allow_loopback:
            raise ValueError("manual_application_url_private_host_rejected")
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise ValueError("manual_application_url_https_required")
    else:
        if parsed.scheme.casefold() != "https":
            raise ValueError("manual_application_url_https_required")
        if port not in {None, 443}:
            raise ValueError("manual_application_url_nonstandard_port")

        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError:
            if normalized_host in {"localhost", "local", "localdomain"} or normalized_host.endswith(
                (".local", ".internal", ".lan")
            ):
                raise ValueError("manual_application_url_private_host_rejected")
        else:
            if not address.is_global:
                raise ValueError("manual_application_url_private_host_rejected")

    try:
        normalized = validate_navigation_url(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("manual_application_url_malformed") from exc
    if not is_lab_loopback and not safe_public_navigation_url(normalized):
        raise ValueError("manual_application_url_public_host_rejected")
    return normalized


def _enumerated_listing_urls(opportunity: Opportunity) -> tuple[str, ...]:
    """Return only URL values persisted by the listing enumerator."""

    try:
        evidence = json.loads(opportunity.resolution_evidence_json or "{}")
    except (TypeError, ValueError):
        return ()
    if not isinstance(evidence, Mapping):
        return ()
    raw_candidates = evidence.get("candidate_roles")
    if not isinstance(raw_candidates, (list, tuple)):
        return ()
    urls: list[str] = []
    for candidate in raw_candidates[:50]:
        if not isinstance(candidate, Mapping):
            continue
        raw_url = candidate.get("url")
        if not isinstance(raw_url, str):
            continue
        try:
            normalized = validate_navigation_url(raw_url)
        except (TypeError, ValueError):
            continue
        if normalized not in urls:
            urls.append(normalized)
    return tuple(urls)


def is_target_resolution_failure(
    application: Application,
    opportunity: Opportunity,
) -> bool:
    """Identify a blocker that can be resolved by a candidate URL.

    Eligibility, conflict, package, and post-application blockers do not get
    this affordance. Explicit target classifications and resolver evidence do.
    """

    if user_status_exclusion_reason(opportunity.user_status) is not None:
        return False
    target = str(getattr(opportunity, "target_status", "") or "UNRESOLVED").upper()
    if target in _MANUAL_ELIGIBLE_KINDS:
        return False
    action = str(getattr(application, "next_action", "") or "")
    try:
        evidence = json.loads(getattr(opportunity, "resolution_evidence_json", "{}") or "{}")
    except (TypeError, ValueError):
        evidence = {}
    evidence_text = ""
    if isinstance(evidence, Mapping):
        try:
            evidence_text = json.dumps(evidence, sort_keys=True).casefold()
        except (TypeError, ValueError):
            evidence_text = str(evidence).casefold()
    combined = f"{target} {action} {evidence_text}".casefold()
    has_identity_signal = any(term in combined for term in _MANUAL_IDENTITY_FAILURE_TERMS)
    has_eligibility_signal = any(term in combined for term in _MANUAL_ELIGIBILITY_TERMS)
    if target in _MANUAL_TARGET_FAILURE_KINDS:
        if target == TargetKind.BLOCKED.value:
            # BLOCKED is also used by ordinary eligibility/package blockers;
            # require explicit resolver evidence before exposing a URL input.
            return has_identity_signal
        if target == TargetKind.UNRESOLVED.value:
            return has_identity_signal or not has_eligibility_signal
        return True
    return has_identity_signal


def _manual_evidence_scalars(
    value: object,
    aliases: frozenset[str],
    *,
    depth: int = 0,
):
    """Yield bounded scalar identity evidence from a typed result."""

    if depth > 12:
        return
    if isinstance(value, Mapping):
        for raw_key, item in list(value.items())[:200]:
            key = normalise_evidence_key(raw_key)
            if key in aliases and not isinstance(
                item, (Mapping, list, tuple, set, frozenset, bool)
            ):
                text = str(item or "").strip()
                if text and text.casefold() not in {"none", "null", "false"}:
                    yield text[:500]
            yield from _manual_evidence_scalars(item, aliases, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:200]:
            yield from _manual_evidence_scalars(item, aliases, depth=depth + 1)


def _manual_identity_failure(
    resolution: TargetResolution,
    opportunity: Opportunity,
    *,
    previous_application_url: str | None,
) -> str:
    """Require typed employer/role identity to match the stored opportunity."""

    if resolution.kind not in {
        TargetKind.APPLICATION_ENTRY,
        TargetKind.APPLICATION_FORM,
    }:
        return ""
    if not resolution.identity_verified:
        return "manual_identity_not_verified"

    expected_employer = _manual_identity_text(opportunity.employer)
    employer_values = tuple(
        dict.fromkeys(
            _manual_identity_text(item)
            for item in _manual_evidence_scalars(
                resolution.evidence,
                frozenset(normalise_evidence_key(key) for key in EMPLOYER_EVIDENCE_KEYS),
            )
            if _manual_identity_text(item)
        )
    )
    if expected_employer and not employer_values:
        return "manual_employer_missing"
    if expected_employer and set(employer_values) != {expected_employer}:
        return "manual_employer_mismatch"

    expected_role = _manual_identity_text(opportunity.role_title)
    role_values = tuple(
        dict.fromkeys(
            _manual_identity_text(item)
            for item in _manual_evidence_scalars(
                resolution.evidence,
                frozenset(normalise_evidence_key(key) for key in ROLE_EVIDENCE_KEYS),
            )
            if _manual_identity_text(item)
        )
    )
    if expected_role and not role_values:
        return "manual_role_missing"
    if expected_role and set(role_values) != {expected_role}:
        return "manual_role_mismatch"

    # If an earlier inspection had a structured requisition identity, retain
    # the exact job binding as an additional contradiction check. A source
    # page without such an identity remains governed by the shared resolver.
    if previous_application_url:
        from app.services.requisition_identity import requisition_identity_from_url

        previous_identity = requisition_identity_from_url(previous_application_url)
        final_identity = requisition_identity_from_url(resolution.final_url)
        if previous_identity is not None and final_identity is not None:
            if previous_identity.canonical_key != final_identity.canonical_key:
                return "manual_requisition_mismatch"
    return ""


def _manual_resolution_candidate(raw: object) -> TargetResolution | None:
    if isinstance(raw, TargetResolution):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        candidate = raw[0]
        return candidate if isinstance(candidate, TargetResolution) else None
    if isinstance(raw, Mapping):
        candidate = raw.get("resolution")
        return candidate if isinstance(candidate, TargetResolution) else None
    return None


def _manual_resolution_with_downgrade(
    raw: object,
    resolution: TargetResolution,
    reason_code: str,
) -> object:
    downgraded = replace(
        resolution,
        kind=TargetKind.UNRESOLVED,
        identity_verified=False,
        form_verified=False,
        reason_codes=tuple(
            dict.fromkeys((*resolution.reason_codes, reason_code))
        ),
    )
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Mapping):
        return downgraded, raw[1]
    if isinstance(raw, Mapping) and "resolution" in raw:
        return {**raw, "resolution": downgraded}
    return downgraded


def _call_capable_manual_resolver(
    configured: object,
    context: object,
    capability: SourceResolutionCapability,
) -> object:
    """Invoke a server-owned resolver while carrying the one-call capability."""

    navigator = getattr(configured, "navigator", None)
    if navigator is not None:
        return NavigatorTargetResolver(
            navigator,
            source_capability=capability,
            headed=False,
        )(context)

    method = getattr(configured, "resolve_application_target", None)
    if callable(method):
        return method(
            context.application_id,
            source_capability=capability,
            headed=False,
        )

    if not callable(configured):
        raise RuntimeError("human target resolver is unavailable")
    try:
        signature = inspect.signature(configured)
    except (TypeError, ValueError):
        return configured(context, source_capability=capability, headed=False)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if "source_capability" in signature.parameters or accepts_kwargs:
        kwargs: dict[str, object] = {"source_capability": capability}
        if "headed" in signature.parameters or accepts_kwargs:
            kwargs["headed"] = False
        return configured(context, **kwargs)
    # Legacy test/lab callbacks are context-only. Production wiring uses the
    # Navigator branch above, which is the only path that can open a browser.
    return configured(context)


class _HumanSuppliedTargetResolver:
    """Pre-bind a typed resolver result to the stored employer and role."""

    def __init__(
        self,
        request: Request,
        capability: SourceResolutionCapability,
        opportunity: Opportunity,
        *,
        previous_application_url: str | None,
    ) -> None:
        self.request = request
        self.capability = capability
        self.opportunity = opportunity
        self.previous_application_url = previous_application_url
        self.failed_check = ""
        self.observed_kind = TargetKind.UNRESOLVED.value

    def __call__(self, context: object) -> object:
        configured = _target_resolver(self.request)
        raw = _call_capable_manual_resolver(configured, context, self.capability)
        candidate = _manual_resolution_candidate(raw)
        if candidate is None:
            return raw
        self.observed_kind = candidate.kind.value
        failure = _manual_identity_failure(
            candidate,
            self.opportunity,
            previous_application_url=self.previous_application_url,
        )
        if failure:
            self.failed_check = failure
            return _manual_resolution_with_downgrade(raw, candidate, failure)
        return raw


def _issue_manual_source_capability(
    normalized_url: str,
    *,
    allow_loopback: bool,
) -> SourceResolutionCapability:
    if allow_loopback and _manual_is_loopback_127(urlsplit(normalized_url).hostname):
        return SourceResolutionCapability(
            source_url=normalized_url,
            hostname=normalise_hostname(urlsplit(normalized_url).hostname or ""),
            origin=origin_for_url(normalized_url),
        )
    return SourceResolutionCapability.issue(normalized_url)


def _manual_failure_check(
    outcome: ResolutionOutcome,
    resolver: _HumanSuppliedTargetResolver,
) -> str:
    if resolver.failed_check:
        return resolver.failed_check
    resolution = outcome.resolution
    # An unresolved result carries the resolver's most useful diagnostic;
    # retain it instead of masking it with the generic UNRESOLVED classifier.
    if resolution is not None and resolution.kind is TargetKind.UNRESOLVED:
        for reason in outcome.reason_codes:
            if reason and reason not in {"phase15_test_observation"}:
                return str(reason)
    if resolution is not None and resolution.kind not in {
        TargetKind.APPLICATION_ENTRY,
        TargetKind.APPLICATION_FORM,
        TargetKind.UNRESOLVED,
    }:
        return f"target_classification_{resolution.kind.value.casefold()}"
    for reason in outcome.reason_codes:
        if reason and reason not in {"phase15_test_observation"}:
            return str(reason)
    return "manual_target_not_verified"


def _record_human_target_audit(
    session,
    application: Application,
    *,
    raw_url: str,
    normalized_url: str | None,
    verification_outcome: Mapping[str, object],
    capability: SourceResolutionCapability | None,
    application_advanced: bool,
    target_kind: str,
    promoted: bool,
) -> None:
    bounded_raw = raw_url[:4096] if isinstance(raw_url, str) else ""
    capability_details: dict[str, object] | None = None
    if capability is not None:
        capability_details = {
            "id": str(getattr(capability, "capability_id", ""))[:128],
            "hostname": normalise_hostname(str(getattr(capability, "hostname", ""))),
            "origin": str(getattr(capability, "origin", ""))[:300],
            "source_url": str(getattr(capability, "source_url", ""))[:2048],
            "single_use": True,
            "revoked": not bool(getattr(capability, "active", False)),
        }
    append_audit(
        session,
        AuditInput(
            "user",
            "opportunity.target_resolution_human_supplied",
            "opportunity",
            application.opportunity_id,
            {
                "actor": "user",
                "input_source": "human_supplied_application_url",
                "typed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "typed_url_present": bool(bounded_raw.strip()),
                "typed_url": normalized_url or "",
                "typed_url_sha256": hashlib.sha256(
                    bounded_raw.encode("utf-8", "replace")
                ).hexdigest(),
                "verification_outcome": dict(verification_outcome),
                "target_kind": str(target_kind or TargetKind.UNRESOLVED.value)[:80],
                "promoted": bool(promoted),
                "application_advanced": bool(application_advanced),
                "submitted": False,
                "employer": str(application.opportunity.employer or "")[:240],
                "role_title": str(application.opportunity.role_title or "")[:320],
                "capability": capability_details,
            },
        ),
    )


def _manual_input_rejection(
    session,
    application: Application,
    *,
    raw_url: str,
    reason: str,
) -> JSONResponse:
    application.next_action = (
        f"Target URL rejected: {reason[:430]}"
    )[:500]
    session.flush()
    _record_human_target_audit(
        session,
        application,
        raw_url=raw_url,
        normalized_url=None,
        verification_outcome={
            "status": "rejected",
            "failed_check": reason[:160],
            "target_kind": TargetKind.UNRESOLVED.value,
        },
        capability=None,
        application_advanced=False,
        target_kind=TargetKind.UNRESOLVED.value,
        promoted=False,
    )
    return JSONResponse(
        status_code=422,
        content={
            "code": "manual_application_url_rejected",
            "message": (
                f"Human-supplied application URL rejected: {reason}. "
                "No application state changed and no submission was made."
            ),
            "detail": reason,
            "submitted": False,
            "application_advanced": False,
        },
    )


def _clear_unverified_manual_url(application: Application) -> bool:
    opportunity = application.opportunity
    if getattr(opportunity, "resolved_at", None) is not None:
        return False
    opportunity.application_url = None
    opportunity.application_url_provenance = ""
    return True


def _reload_and_clear_unverified_manual_url(
    session,
    application_id: str,
) -> Application | None:
    """Persist cleanup after the staged inspection transaction was committed."""

    session.rollback()
    refreshed = session.get(Application, application_id)
    if refreshed is not None and _clear_unverified_manual_url(refreshed):
        session.flush()
        # The caller may need to raise an HTTP error after this point. Commit
        # the cleanup before that error is propagated to the request scope.
        session.commit()
    return refreshed


def _resolve_human_supplied_blocker(
    application: Application,
    request: Request,
    session,
    body: BlockerResolutionPayload,
    *,
    advance_after_resolution: bool = True,
) -> JSONResponse:
    """Resolve only a target blocker with one bounded, human URL attempt."""

    raw_url = body.application_url or ""
    allow_loopback = _manual_target_lab_enabled(request)
    try:
        normalized_url = validate_human_supplied_application_url(
            raw_url,
            allow_loopback=allow_loopback,
        )
        capability = _issue_manual_source_capability(
            normalized_url,
            allow_loopback=allow_loopback,
        )
    except (TypeError, ValueError) as exc:
        return _manual_input_rejection(
            session,
            application,
            raw_url=raw_url,
            reason=str(exc)[:160] or "manual_application_url_invalid",
        )

    opportunity = application.opportunity
    previous_application_url = opportunity.application_url
    allow_not_open = not opportunity.is_open_for_applications
    manual_resolver = _HumanSuppliedTargetResolver(
        request,
        capability,
        opportunity,
        previous_application_url=previous_application_url,
    )
    try:
        # Navigator performs a fresh authoritative database read in its owner
        # thread before creating a worker. Commit only this validated,
        # unverified inspection URL so that read sees the exact candidate;
        # target promotion remains exclusively inside TargetResolutionService.
        opportunity.application_url = normalized_url
        opportunity.application_url_provenance = "OPERATOR_SUPPLIED"
        session.flush()
        session.commit()
        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=manual_resolver,
            allow_not_open=allow_not_open,
        )
    except UserStatusAutomationExcludedError as exc:
        refreshed = _reload_and_clear_unverified_manual_url(session, application.id)
        if refreshed is not None:
            application = refreshed
        capability.revoke()
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        refreshed = _reload_and_clear_unverified_manual_url(session, application.id)
        if refreshed is not None:
            application = refreshed
        capability.revoke()
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - unexpected inspection fails closed
        refreshed = _reload_and_clear_unverified_manual_url(session, application.id)
        if refreshed is not None:
            application = refreshed
        capability.revoke()
        application.next_action = "Target verification failed: manual_resolver_failed"
        session.flush()
        _record_human_target_audit(
            session,
            application,
            raw_url=raw_url,
            normalized_url=normalized_url,
            verification_outcome={
                "status": "rejected",
                "failed_check": "manual_resolver_failed",
                "error_type": type(exc).__name__.casefold()[:120],
                "target_kind": TargetKind.UNRESOLVED.value,
            },
            capability=capability,
            application_advanced=False,
            target_kind=TargetKind.UNRESOLVED.value,
            promoted=False,
        )
        return JSONResponse(
            status_code=409,
            content={
                "code": "manual_target_resolution_failed",
                "message": (
                    "The supplied URL could not be verified by the server-owned "
                    "target resolver. The application remains BLOCKED and no "
                    "submission was made."
                ),
                "detail": "manual_resolver_failed",
                "submitted": False,
                "application_advanced": False,
            },
        )
    finally:
        # The capability must remain active until the Navigator owner has
        # returned, then becomes unusable even if the outcome was negative.
        capability.revoke()

    final_application = application
    package = None
    advance_failure = ""
    reason_codes = list(outcome.reason_codes)
    application_advanced = False
    if outcome.promoted and advance_after_resolution:
        service = ApplicationService(
            session,
            request.app.state.settings,
            request.app.state.crypto,
        )
        try:
            decision = service.evaluate(opportunity.id)
            reason_codes.extend(decision.reason_codes)
            if not decision.eligible or decision.conflict_blocked:
                final_application = decision.application
                advance_failure = "application_eligibility_or_conflict_gate"
            else:
                queued = service.queue(application.id)
                package = service.prepare(application.id)
                final_application = package.application
                reason_codes.extend(package.reason_codes)
                application_advanced = final_application.state != ApplicationState.BLOCKED.value
                if not application_advanced:
                    advance_failure = "application_package_gate"
        except (KeyError, ApplicationBlockedError):
            # A verified target is not submission authority. Preserve the
            # target result, report the downstream application gate, and keep
            # the response truthful without attempting a retry automatically.
            session.refresh(application)
            final_application = application
            advance_failure = "application_post_resolution_gate"

    resolution = outcome.resolution
    target_kind = (
        resolution.kind.value
        if resolution is not None
        else TargetKind.UNRESOLVED.value
    )
    failed_check = (
        advance_failure
        if outcome.promoted and advance_after_resolution and not application_advanced
        else _manual_failure_check(outcome, manual_resolver)
        if not outcome.promoted
        else ""
    )
    retain_staged_manual_url = bool(
        allow_not_open
        and any(
            str(reason).startswith("application_window_")
            for reason in outcome.reason_codes
        )
    )
    verification_status = (
        "verified"
        if outcome.promoted
        else "recorded_not_open"
        if retain_staged_manual_url
        else "rejected"
    )
    verification = {
        "status": verification_status,
        "failed_check": failed_check,
        "target_kind": target_kind,
        "reason_codes": list(dict.fromkeys(str(item) for item in reason_codes if item)),
    }
    if not outcome.promoted:
        if not retain_staged_manual_url and _clear_unverified_manual_url(final_application):
            # ``TargetResolutionService`` intentionally supports retaining an
            # older verified target. This attempt had no such proof, so the
            # staged human URL must remain only in the bounded audit record.
            opportunity = final_application.opportunity
        final_application.next_action = (
            "Human-supplied target recorded; application window is "
            f"{opportunity.application_window_status or 'UNKNOWN'}. "
            "Recheck when the window opens; no submission was made."
            if retain_staged_manual_url
            else f"Target verification failed: {failed_check}"
        )[:500]
        session.flush()
    _record_human_target_audit(
        session,
        final_application,
        raw_url=raw_url,
        normalized_url=normalized_url,
        verification_outcome=verification,
        capability=capability,
        application_advanced=application_advanced,
        target_kind=target_kind,
        promoted=outcome.promoted,
    )

    state = final_application.state
    if outcome.promoted and application_advanced:
        message = (
            f"Verified {opportunity.employer} — {opportunity.role_title} as "
            f"{target_kind}; application advanced to {state}. No submission was made."
        )
    elif outcome.promoted and not advance_after_resolution:
        message = (
            f"Verified {opportunity.employer} — {opportunity.role_title} as "
            f"{target_kind}; the selected listing target was recorded. "
            "No submission was made."
        )
    elif outcome.promoted:
        message = (
            f"Verified {opportunity.employer} — {opportunity.role_title} as "
            f"{target_kind}, but the application remains {state} because "
            f"{failed_check}. No submission was made."
        )
    elif retain_staged_manual_url:
        message = (
            f"Human-supplied target recorded for {opportunity.employer} — "
            f"{opportunity.role_title}; application window is "
            f"{opportunity.application_window_status or 'UNKNOWN'}. "
            "No submission was made."
        )
    else:
        message = (
            f"Target verification failed at {failed_check}; the application remains "
            "BLOCKED. No submission was made."
        )
    payload = {
        **_application(final_application),
        "application_id": final_application.id,
        "opportunity_id": opportunity.id,
        "target_kind": target_kind,
        "promoted": bool(outcome.promoted),
        "human_supplied": True,
        "application_advanced": application_advanced,
        "rechecked": True,
        "resolution_recorded": True,
        "submitted": False,
        "ready": bool(package.ready) if package is not None else False,
        "cv_id": package.cv_id if package is not None else None,
        "cover_letter_id": package.cover_letter_id if package is not None else None,
        "reason_codes": list(dict.fromkeys(str(item) for item in reason_codes if item)),
        "verification": verification,
        "message": message,
        "human_handoff_required": outcome.human_handoff_required,
        "next_action": outcome.next_action,
        "handoff": dict(outcome.handoff),
    }
    return JSONResponse(
        status_code=(
            200
            if outcome.promoted and (application_advanced or not advance_after_resolution)
            else 202
        ),
        content=payload,
    )


def _ensure_application_for_target(
    request: Request,
    opportunity: Opportunity,
    session,
    requested_application_id: str | None,
) -> Application:
    if requested_application_id:
        application = session.get(Application, requested_application_id)
        if application is None:
            raise HTTPException(404, "Application not found")
        if application.opportunity_id != opportunity.id:
            raise HTTPException(409, "Application is not bound to this opportunity")
        return application
    application = session.scalar(
        select(Application).where(Application.opportunity_id == opportunity.id)
    )
    if application is not None:
        return application
    try:
        decision = ApplicationService(
            session, request.app.state.settings, request.app.state.crypto
        ).evaluate(opportunity.id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return decision.application


def _target_resolution_payload(outcome: ResolutionOutcome) -> dict[str, object]:
    record = outcome.opportunity
    return {
        "id": record.id,
        "opportunity_id": record.id,
        "application_id": outcome.application_id,
        "source_url": record.url,
        "source_fingerprint": record.source_fingerprint,
        "application_url": outcome.application_url,
        "automation_url": record.automation_url,
        "target_status": outcome.target_status,
        "resolved_ats_type": record.resolved_ats_type,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
        "resolution_attempted_at": (
            record.resolution_attempted_at.isoformat()
            if record.resolution_attempted_at
            else None
        ),
        "promoted": outcome.promoted,
        "human_handoff_required": outcome.human_handoff_required,
        "next_action": outcome.next_action,
        "handoff": dict(outcome.handoff),
        "reason_codes": list(outcome.reason_codes),
    }


def _resolve_target_for_opportunity(
    opportunity_id: str,
    request: Request,
    session: SessionDep,
    body: ResolveTargetPayload | None,
    *,
    application_id: str | None = None,
) -> JSONResponse:
    if body is not None:
        if body.opportunity_id and body.opportunity_id != opportunity_id:
            raise HTTPException(409, "Exact opportunity_id does not match the route")
        if application_id and body.application_id and body.application_id != application_id:
            raise HTTPException(409, "Exact application_id does not match the route")
        if body.application_id:
            requested_application_id = body.application_id
        else:
            requested_application_id = application_id
    else:
        requested_application_id = application_id

    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(404, "Opportunity not found")
    status_reason = user_status_exclusion_reason(opportunity.user_status)
    if status_reason is not None:
        raise HTTPException(409, status_reason)
    application = _ensure_application_for_target(
        request, opportunity, session, requested_application_id
    )
    if application_id and application.id != application_id:
        raise HTTPException(409, "Application is not bound to this opportunity")
    # Target resolution never queues or submits.  Re-run eligibility and
    # conflict checks immediately before opening a Navigator/human workflow so
    # a stale application state cannot grant a new action.
    try:
        ApplicationService(
            session, request.app.state.settings, request.app.state.crypto
        ).evaluate(opportunity.id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        outcome = TargetResolutionService(session).resolve(
            opportunity.id,
            application_id=application.id,
            resolver=_target_resolver(request),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        # Malformed resolver output is a safety conflict, never a successful
        # target resolution.  The service's persisted evidence remains the
        # last truthful state; callers cannot smuggle a raw URL through this
        # boundary.
        raise HTTPException(409, str(exc)) from exc
    except UserStatusAutomationExcludedError as exc:
        raise HTTPException(409, str(exc)) from exc
    payload = _target_resolution_payload(outcome)
    return JSONResponse(status_code=200 if outcome.promoted else 202, content=payload)


@router.post("/opportunities/{opportunity_id}/resolve-target")
def resolve_opportunity_target(
    opportunity_id: str,
    request: Request,
    session: SessionDep,
    body: ResolveTargetPayload | None = Body(default=None),
) -> JSONResponse:
    return _resolve_target_for_opportunity(opportunity_id, request, session, body)


@router.post("/opportunities/{opportunity_id}/resolve")
def resolve_opportunity_target_alias(
    opportunity_id: str,
    request: Request,
    session: SessionDep,
    body: ResolveTargetPayload | None = Body(default=None),
) -> JSONResponse:
    return _resolve_target_for_opportunity(opportunity_id, request, session, body)


@router.get("/applications")
def list_applications(session: SessionDep) -> list[dict[str, object]]:
    records = list(session.scalars(select(Application).order_by(Application.priority.desc())).all())
    return [_application(item) for item in records]


@router.post("/applications/{application_id}/resolve-target")
def resolve_application_target(
    application_id: str,
    request: Request,
    session: SessionDep,
    body: ResolveTargetPayload | None = Body(default=None),
) -> JSONResponse:
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    if body is not None and body.application_id and body.application_id != application_id:
        raise HTTPException(409, "Exact application_id does not match the route")
    if body is not None and body.opportunity_id and body.opportunity_id != application.opportunity_id:
        raise HTTPException(409, "Application is not bound to the supplied opportunity")
    return _resolve_target_for_opportunity(
        application.opportunity_id,
        request,
        session,
        body,
        application_id=application_id,
    )


@router.post("/applications/{application_id}/resolve")
def resolve_application_target_alias(
    application_id: str,
    request: Request,
    session: SessionDep,
    body: ResolveTargetPayload | None = Body(default=None),
) -> JSONResponse:
    return resolve_application_target(application_id, request, session, body)


@router.post("/applications/{application_id}/choose-target", response_model=None)
def choose_listing_target(
    application_id: str,
    request: Request,
    session: SessionDep,
    body: ListingChoicePayload = Body(...),
) -> JSONResponse:
    """Resolve exactly one role URL previously enumerated from a listing.

    The URL is not a free-form destination: it must be an exact persisted
    candidate, and the existing human-supplied URL path performs the fresh
    server-owned identity checks.  This endpoint never queues or submits.
    """

    if body.application_id != application_id:
        raise HTTPException(
            409,
            "Action-time application confirmation must match the exact application",
        )
    if body.confirmed is not True:
        raise HTTPException(
            409,
            "Explicit confirmation is required to choose an enumerated role",
        )
    resolution_reason = body.resolution_reason.strip()
    if len(resolution_reason) < 8:
        raise HTTPException(
            422,
            "A non-empty role-selection reason of at least 8 characters is required",
        )
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, f"Application not found: {application_id}")
    opportunity = application.opportunity
    exclusion_reason = user_status_exclusion_reason(opportunity.user_status)
    if exclusion_reason is not None:
        raise HTTPException(
            409,
            f"{exclusion_reason}; human role selection is unavailable and no action is required",
        )
    if application.state not in {
        ApplicationState.DISCOVERED.value,
        ApplicationState.NEEDS_USER.value,
        ApplicationState.BLOCKED.value,
        ApplicationState.FAILED_RETRYABLE.value,
    }:
        raise HTTPException(
            409,
            "Role selection is only available before the guarded submission workflow "
            f"(current: {application.state})",
        )
    target_status = str(opportunity.target_status or "").upper()
    candidates = _enumerated_listing_urls(opportunity)
    if target_status != TargetKind.MULTIPLE_CANDIDATE_ROLES.value or not candidates:
        raise HTTPException(
            409,
            "This application has no persisted multiple-candidate listing requiring a human choice",
        )
    try:
        normalized_url = validate_human_supplied_application_url(
            body.application_url,
            allow_loopback=_manual_target_lab_enabled(request),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)[:160] or "manual_application_url_invalid") from exc
    if normalized_url not in candidates:
        raise HTTPException(
            409,
            "The selected application URL was not enumerated for this listing; choose one of the persisted candidates",
        )
    return _resolve_human_supplied_blocker(
        application,
        request,
        session,
        BlockerResolutionPayload(
            application_id=application_id,
            application_url=normalized_url,
            resolution_reason=resolution_reason,
            confirmed=True,
        ),
        advance_after_resolution=False,
    )


class OperatorSuppliedUrlPayload(BaseModel):
    """Operator-supplied application URL for the fix-link affordance.

    No resolution reason or confirmation is required. The operator confirms the
    URL simply by submitting it; the server validates and stores it with
    OPERATOR_SUPPLIED provenance distinct from resolver-supplied URLs.
    """

    model_config = ConfigDict(extra="forbid")

    application_url: str = Field(min_length=8, max_length=2048)


@router.post("/applications/{application_id}/operator-supplied-url", response_model=None)
def operator_supplied_application_url(
    application_id: str,
    request: Request,
    session: SessionDep,
    body: OperatorSuppliedUrlPayload = Body(...),
) -> dict[str, object] | JSONResponse:
    """Accept an operator-corrected application URL and queue it for the SAME
    verification the resolver applies to any other candidate URL.

    A human typing a URL establishes intent, not identity.  Syntax validation
    proves only that the string is a well-formed https URL -- it does not prove
    the page belongs to this employer, or that it is an application form at all.
    So this endpoint stores the URL with OPERATOR_SUPPLIED provenance and leaves
    the target UNRESOLVED; the resolver inspects ``application_url`` in
    preference to ``source_url``, so the next resolution pass verifies exactly
    what the operator supplied and promotes it only if it genuinely verifies.

    Promoting straight to APPLICATION_FORM on syntax alone would let ARGUS fill
    the candidate's real name, email, phone and CV into a page nothing had
    confirmed was the right employer's form.
    """
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, f"Application not found: {application_id}")
    opportunity = application.opportunity
    raw_url = body.application_url or ""
    allow_loopback = _manual_target_lab_enabled(request)
    try:
        normalized_url = validate_human_supplied_application_url(
            raw_url,
            allow_loopback=allow_loopback,
        )
    except (TypeError, ValueError) as exc:
        reason = str(exc)[:160] or "operator_supplied_url_invalid"
        _record_operator_supplied_url_audit(
            session,
            application,
            raw_url=raw_url,
            normalized_url=None,
            verification_outcome={
                "status": "rejected",
                "failed_check": reason,
                "target_kind": opportunity.target_status,
            },
            promoted=False,
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "operator_supplied_url_invalid",
                "message": f"The supplied URL could not be validated: {reason}",
                "application_url_supplied": False,
            },
        )

    previous_url = opportunity.application_url
    previous_provenance = opportunity.application_url_provenance
    previous_target_status = opportunity.target_status

    opportunity.application_url = normalized_url
    opportunity.application_url_provenance = "OPERATOR_SUPPLIED"
    # Deliberately NOT promoted here, and resolved_at is deliberately NOT
    # stamped: nothing has been resolved yet.  UNRESOLVED is what makes the
    # resolver pick this row up and verify the operator's URL for real.
    opportunity.target_status = TargetKind.UNRESOLVED.value
    opportunity.resolved_at = None
    session.flush()

    _record_operator_supplied_url_audit(
        session,
        application,
        raw_url=raw_url,
        normalized_url=normalized_url,
        verification_outcome={
            # "verified" would be a false claim: no page was fetched and no
            # identity was checked.  The audit chain records what actually
            # happened -- the URL was accepted and is awaiting verification.
            "status": "stored_pending_verification",
            "target_kind": TargetKind.UNRESOLVED.value,
            "previous_url": previous_url,
            "previous_provenance": previous_provenance,
            "previous_target_status": previous_target_status,
        },
        promoted=False,
    )
    session.flush()

    return {
        "application_id": application_id,
        "application_url": normalized_url,
        "application_url_provenance": "OPERATOR_SUPPLIED",
        "target_status": TargetKind.UNRESOLVED.value,
        "verified": False,
        "pending_verification": True,
        "message": (
            "Link saved. ARGUS will verify it on the next resolution pass "
            "before filling anything into it."
        ),
        "submitted": False,
    }


def _record_operator_supplied_url_audit(
    session,
    application: Application,
    *,
    raw_url: str,
    normalized_url: str | None,
    verification_outcome: Mapping[str, object],
    promoted: bool,
) -> None:
    """Record an operator-supplied URL correction in the audit chain."""
    bounded_raw = raw_url[:4096] if isinstance(raw_url, str) else ""
    append_audit(
        session,
        AuditInput(
            "user",
            "opportunity.operator_supplied_url",
            "opportunity",
            application.opportunity_id,
            {
                "actor": "user",
                "input_source": "operator_supplied_application_url",
                "typed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "typed_url_present": bool(bounded_raw.strip()),
                "typed_url": normalized_url or "",
                "typed_url_sha256": hashlib.sha256(
                    bounded_raw.encode("utf-8", "replace")
                ).hexdigest(),
                "verification_outcome": dict(verification_outcome),
                "promoted": bool(promoted),
                "submitted": False,
                "employer": str(application.opportunity.employer or "")[:240],
                "role_title": str(application.opportunity.role_title or "")[:320],
            },
        ),
    )


@router.post("/applications/{application_id}/queue")
def queue_application(
    application_id: str, request: Request, session: SessionDep
) -> dict[str, object]:
    try:
        record = ApplicationService(
            session, request.app.state.settings, request.app.state.crypto
        ).queue(application_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ApplicationBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _application(record)


@router.post("/applications/{application_id}/resolve-blocker", response_model=None)
def resolve_application_blocker(
    application_id: str,
    request: Request,
    session: SessionDep,
    body: BlockerResolutionPayload = Body(...),
) -> dict[str, object] | JSONResponse:
    """Record a human blocker resolution and re-run checks without submitting.

    This endpoint intentionally does not delegate to ``queue`` directly from
    the UI.  It binds the reason and confirmation to the path id.  Ordinary
    blocker records must be BLOCKED; a pre-submission target-resolution
    failure may use the same human-supplied URL path while DISCOVERED,
    NEEDS_USER, or FAILED_RETRYABLE.  It re-evaluates eligibility/conflicts,
    then queues and prepares only when those server-side checks pass.  A
    missing package leaves the application in NEEDS_USER for human follow-up.
    """

    if body.application_id != application_id:
        raise HTTPException(
            409,
            "Action-time application confirmation must match the exact blocked application",
        )
    if body.confirmed is not True:
        raise HTTPException(
            409,
            "Explicit confirmation is required to record this blocker resolution",
        )
    resolution_reason = body.resolution_reason.strip()
    if len(resolution_reason) < 8:
        raise HTTPException(
            422,
            "A non-empty blocker resolution reason of at least 8 characters is required",
        )
    application = session.get(Application, application_id)
    if application is None:
        raise HTTPException(404, f"Application not found: {application_id}")
    opportunity = application.opportunity
    target_failure = is_target_resolution_failure(application, opportunity)
    pre_submission_target_states = {
        ApplicationState.DISCOVERED.value,
        ApplicationState.NEEDS_USER.value,
        ApplicationState.BLOCKED.value,
        ApplicationState.FAILED_RETRYABLE.value,
    }
    if application.state != ApplicationState.BLOCKED.value and (
        not target_failure
        or body.application_url is None
        or application.state not in pre_submission_target_states
    ):
        raise HTTPException(
            409,
            "Blocker resolution is only valid for BLOCKED applications, or for a "
            "pre-submission target-resolution failure with a human-supplied URL "
            f"(current: {application.state})",
        )
    if body.application_url is not None:
        if user_status_exclusion_reason(opportunity.user_status) is not None:
            raise HTTPException(
                409,
                f"{user_status_exclusion_reason(opportunity.user_status)}; "
                "human-supplied target resolution is unavailable and no action is required",
            )
        if not target_failure:
            raise HTTPException(
                409,
                "Human-supplied application URLs are accepted only for target-resolution failures",
            )
        return _resolve_human_supplied_blocker(application, request, session, body)
    if target_failure:
        if user_status_exclusion_reason(opportunity.user_status) is not None:
            raise HTTPException(
                409,
                f"{user_status_exclusion_reason(opportunity.user_status)}; no action is required",
            )
        raise HTTPException(
            409,
            "This blocker is a target-resolution failure. Paste the real application URL; "
            "the record-only action cannot clear it without target verification",
        )
    service = ApplicationService(
        session, request.app.state.settings, request.app.state.crypto
    )
    try:
        decision = service.evaluate(application.opportunity_id)
        append_audit(
            session,
            AuditInput(
                "user",
                "application.blocker_resolution_recorded",
                "application",
                application_id,
                {
                    "resolution_reason": resolution_reason,
                    "eligibility_rechecked": True,
                    "conflict_rechecked": True,
                    "submission_started": False,
                },
            ),
        )
        if not decision.eligible or decision.conflict_blocked:
            return {
                **_application(decision.application),
                "application_id": decision.application.id,
                "rechecked": True,
                "submitted": False,
                "reason_codes": decision.reason_codes,
                "resolution_recorded": True,
            }
        queued = service.queue(application_id)
        package = service.prepare(application_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ApplicationBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        **_application(queued),
        "application_id": queued.id,
        "state": package.application.state,
        "ready": package.ready,
        "cv_id": package.cv_id,
        "cover_letter_id": package.cover_letter_id,
        "reason_codes": package.reason_codes,
        "rechecked": True,
        "submitted": False,
        "resolution_recorded": True,
    }


@router.post("/applications/{application_id}/prepare")
def prepare_application(
    application_id: str, request: Request, session: SessionDep
) -> dict[str, object]:
    try:
        package = ApplicationService(
            session, request.app.state.settings, request.app.state.crypto
        ).prepare(application_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ApplicationBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "application_id": package.application.id,
        "state": package.application.state,
        "ready": package.ready,
        "cv_id": package.cv_id,
        "cover_letter_id": package.cover_letter_id,
        "reason_codes": package.reason_codes,
    }


@router.post(
    "/applications/{application_id}/prefill",
    status_code=202,
    response_model=None,
)
def prefill_application(
    application_id: str,
    request: Request,
) -> dict[str, object] | JSONResponse:
    """Start one candidate-owned, headed PREFILL journey.

    This route intentionally has no caller-selected mode, URL, headed flag,
    submit body, authority, or confirmation input.  Its only runner call is
    the literal PREFILL mode below; the generic legacy run route remains a
    separate compatibility surface.
    """

    lock = _prefill_lock(application_id)
    already_in_flight = not lock.acquire(blocking=False)
    if already_in_flight:
        lock.acquire()
    try:
        navigator = getattr(request.app.state, "navigator", None)
        with request.app.state.db.session_scope() as guard_session:
            application = guard_session.get(Application, application_id)
            if application is None:
                raise HTTPException(404, f"Application not found: {application_id}")
            opportunity = application.opportunity
            try:
                active = (
                    navigator.active_for_application(application_id)
                    if navigator is not None
                    else None
                )
            except Exception as exc:  # noqa: BLE001 - fail closed before browser use
                return _prefill_error(
                    application_id,
                    "navigator_state_unavailable",
                    "The live Navigator state could not be verified; no browser was started.",
                )

            if already_in_flight:
                if active is not None:
                    return _prefill_error(
                        application_id,
                        "prefill_session_active",
                        "A live Navigator session already exists for this application; use Continue rather than starting another run.",
                        continue_payload=_prefill_continue_payload(active),
                    )
                return _prefill_error(
                    application_id,
                    "prefill_request_already_completed",
                    "Another preparation click for this application already completed; refresh the application to see its current state.",
                )

            reason = prefill_start_reason(
                target_status=opportunity.target_status,
                user_status=opportunity.user_status,
                application_state=application.state,
                live_session=active is not None,
            )
            if reason is not None:
                code, wall = reason
                return _prefill_error(
                    application_id,
                    code,
                    wall,
                    continue_payload=(
                        _prefill_continue_payload(active)
                        if active is not None
                        else None
                    ),
                )

            try:
                # Validate the row-owned destination before any package or
                # browser work.  The runner independently derives the same
                # immutable host scope immediately before Navigator.start().
                row_application_url_allowlist(opportunity.application_url)
            except PrefillBlocked as exc:
                return _prefill_error(application_id, exc.code, str(exc))

            if application.state == ApplicationState.NEEDS_USER.value:
                try:
                    package = ApplicationService(
                        guard_session,
                        request.app.state.settings,
                        request.app.state.crypto,
                    ).prepare(application_id)
                except ApplicationBlockedError as exc:
                    return _prefill_error(
                        application_id,
                        "prefill_package_refused",
                        str(exc),
                    )
                if not package.ready:
                    reason_code = str(
                        package.reason_codes[0]
                        if package.reason_codes
                        else "prefill_package_not_ready"
                    )
                    wall = (
                        f"{reason_code}: upload and approve the required documents "
                        "before starting visible PREFILL."
                    )
                    return _prefill_error(application_id, reason_code, wall)

            if (
                bool(opportunity.cv_required)
                and not application.selected_cv_id
            ):
                return _prefill_error(
                    application_id,
                    "required_cv_missing",
                    "required_cv_missing: upload and approve a CV before starting visible PREFILL.",
                )

            append_audit(
                guard_session,
                AuditInput(
                    "user",
                    "application.prefill_started",
                    "application",
                    application_id,
                    {
                        "mode": RunMode.PREFILL.value,
                        "headed": True,
                        "target_status": PREFILL_TARGET_STATUS,
                    },
                ),
            )

        runner = AutomationRunner(
            request.app.state.db,
            request.app.state.settings,
            request.app.state.crypto,
            handoff_manager=getattr(request.app.state, "handoff_manager", None),
        )
        try:
            # This is deliberately a constant: no query string or request
            # body can turn the candidate start into SUBMIT.
            outcome = runner.run(
                application_id,
                RunMode.PREFILL,
                headed=True,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SubmissionBlocked as exc:
            active = (
                navigator.active_for_application(application_id)
                if navigator is not None
                else None
            )
            if active is not None:
                return _prefill_error(
                    application_id,
                    "prefill_session_active",
                    "A live Navigator session already exists for this application; use Continue rather than starting another run.",
                    continue_payload=_prefill_continue_payload(active),
                )
            return _prefill_error(application_id, exc.code, str(exc))
        except DuplicateSessionError as exc:
            active = (
                navigator.active_for_application(application_id)
                if navigator is not None
                else None
            )
            return _prefill_error(
                application_id,
                "prefill_session_active" if active is not None else "prefill_start_refused",
                (
                    "A live Navigator session already exists for this application; use Continue rather than starting another run."
                    if active is not None
                    else str(exc)
                ),
                continue_payload=(
                    _prefill_continue_payload(active) if active is not None else None
                ),
            )
        except ValueError as exc:
            return _prefill_error(application_id, "prefill_start_refused", str(exc))
        return _prefill_outcome_payload(request, application_id, outcome)
    finally:
        lock.release()


@router.post("/applications/{application_id}/run")
def run_application(
    application_id: str,
    request: Request,
    mode: RunMode = RunMode.DRY_RUN,
    headed: bool = False,
    body: RunApplicationBody | None = Body(default=None),
) -> dict[str, object]:
    if mode is RunMode.SUBMIT and (body is None or body.application_id != application_id):
        raise HTTPException(
            409,
            "Action-time JSON confirmation is required for this exact application before submit",
        )
    with request.app.state.db.SessionLocal() as guard_session:
        status_row = guard_session.execute(
            select(Opportunity.user_status)
            .join(Application, Application.opportunity_id == Opportunity.id)
            .where(Application.id == application_id)
        ).first()
        status_reason = user_status_exclusion_reason(
            status_row[0]
        ) if status_row is not None else None
        if status_reason is not None:
            raise HTTPException(409, status_reason)
    runner = AutomationRunner(
        request.app.state.db,
        request.app.state.settings,
        request.app.state.crypto,
        handoff_manager=getattr(request.app.state, "handoff_manager", None),
    )
    try:
        outcome = runner.run(
            application_id,
            mode,
            headed=headed,
            session_id=body.session_id if mode is RunMode.SUBMIT and body else "",
            authority_id=body.authority_id if mode is RunMode.SUBMIT and body else "",
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except SubmissionBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "run_id": outcome.run_id,
        "state": outcome.state,
        "risk_level": outcome.risk_level,
        "adapter": outcome.adapter,
        "blocked_reasons": outcome.blocked_reasons,
        "receipt": (
            {
                "reference": outcome.receipt.reference,
                "url": outcome.receipt.url,
                "confirmation_text": outcome.receipt.confirmation_text,
            }
            if outcome.receipt
            else None
        ),
        "trace_path": outcome.trace_path,
        "screenshot_path": outcome.screenshot_path,
        # The Navigator UI binds and polls this exact owner-thread session.
        # Keep the legacy handoff key for non-browser clients while exposing
        # the normal session field consumed by the UI state machine.
        "session_id": outcome.handoff_session_id,
        "handoff_session_id": outcome.handoff_session_id,
        "human_boundary": outcome.human_boundary,
    }


@router.get("/runs")
def list_runs(session: SessionDep) -> list[dict[str, object]]:
    records = list(session.scalars(select(AutomationRun).order_by(AutomationRun.created_at.desc())).all())
    return [
        {
            "id": item.id,
            "application_id": item.application_id,
            "mode": item.mode,
            "state": item.state,
            "adapter": item.adapter,
            "risk_level": item.risk_level,
            "trace_path": item.trace_path,
            "screenshot_path": item.screenshot_path,
            "receipt": json.loads(item.receipt_json or "{}"),
            "error": item.error,
        }
        for item in records
    ]


@router.get("/audit")
def list_audit(session: SessionDep) -> dict[str, object]:
    records = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(500)).all())
    verification = verify_audit_chain(session)
    return {
        "verification": {
            "valid": verification.valid,
            "checked_events": verification.checked_events,
            "broken_event_id": verification.broken_event_id,
        },
        "events": [
            {
                "id": item.id,
                "created_at": item.created_at,
                "actor": item.actor,
                "event_type": item.event_type,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "details": json.loads(item.details_json),
                "event_hash": item.event_hash,
            }
            for item in records
        ],
    }


@router.post("/capture")
def capture_opportunity(
    payload: CapturePayload,
    request: Request,
    response: Response,
    session: SessionDep,
    x_argus_token: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    expected_token = request.app.state.settings.api_token
    if not x_argus_token or not secrets.compare_digest(x_argus_token, expected_token):
        raise HTTPException(401, "Invalid ARGUS token")
    service = OpportunityService(session)
    candidate = Opportunity(**payload.model_dump())
    try:
        existing = service.find_exact(candidate)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if existing:
        response.status_code = 200
        return _opportunity(existing)
    record = service.add(candidate, actor="browser_extension")
    response.status_code = 201
    return _opportunity(record)


@router.get("/lab/submissions")
def lab_submissions(request: Request) -> list[dict[str, object]]:
    return list(reversed(request.app.state.lab_submissions))
