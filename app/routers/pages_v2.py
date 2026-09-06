"""Flag-gated Phase 20 presentation routes.

This module owns no submission authority.  It projects server truth into the
V2 templates and may request a stricter/equal runtime automation mode, while
the existing server-side gates remain authoritative.
"""
from __future__ import annotations

import calendar as month_calendar
import json
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.automation.types import RunMode
from app.config import AutomationMode
from app.domain.states import (
    UserApplicationStatus,
    user_status_exclusion_reason,
    user_status_is_automation_eligible,
)
from app.domain.targets import TargetKind, validate_navigation_url
from app.models import (
    Application,
    AuditEvent,
    AutomationRun,
    ConflictRule,
    Document,
    EmailMessage,
    Opportunity,
    OpportunityArchive,
    QuestionRecord,
)
from app.routers.api import is_target_resolution_failure
from app.security.audit import AuditInput, append_audit
from app.services.answers import AnswerService
from app.services.applications import ApplicationBlockedError, ApplicationService
from app.services.profile import ProfileService
from app.services.prefill import (
    HUMAN_BOUNDARY_COPY,
    PREFILL_APPLICATION_STATES,
    PREFILL_TARGET_STATUS,
    prefill_ui_allowed,
    prefill_wall,
    sanitise_blocked_requests,
)


router = APIRouter(tags=["pages-v2"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_LOCAL_TIMEZONE_NAME = "Europe/London"
try:
    _LOCAL_TIMEZONE = ZoneInfo(_LOCAL_TIMEZONE_NAME)
except ZoneInfoNotFoundError:  # Keep flag-off startup safe on minimal Windows builds.
    _LOCAL_TIMEZONE = timezone.utc
_WINDOW_STATE_ORDER = ("OPEN", "NOT_YET_OPEN", "CLOSED", "UNKNOWN")
_WINDOW_STATES = frozenset(_WINDOW_STATE_ORDER)
_USER_STATUS_ORDER = tuple(status.value for status in UserApplicationStatus)
_ACTIVE_PROCESS_USER_STATUSES = frozenset(
    {
        UserApplicationStatus.APPLICATION_SUBMITTED.value,
        UserApplicationStatus.ONLINE_ASSESSMENT.value,
        UserApplicationStatus.HIREVUE.value,
        UserApplicationStatus.ONLINE_TEST.value,
        UserApplicationStatus.FIRST_ROUND.value,
        UserApplicationStatus.OFFER.value,
    }
)
_NEEDS_STATES = frozenset(
    {
        "NEEDS_USER",
        "FILLING",
        "NEEDS_OA",
        "BLOCKED",
        "READY_TO_SUBMIT",
        "FAILED_RETRYABLE",
        "SUBMISSION_UNKNOWN",
    }
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class _PipelineBulkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["archive", "unarchive", "requeue"]
    opportunity_ids: list[str] = Field(min_length=1, max_length=100)
    expected_count: int = Field(ge=1, le=100)
    confirmed: bool = False


def ui_v2_enabled(request: Request) -> bool:
    explicit = getattr(request.app.state, "ui_v2_enabled", None)
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("ARGUS_UI_V2", "").strip().casefold() in _TRUE_VALUES


def _require_v2(request: Request) -> None:
    if not ui_v2_enabled(request):
        raise HTTPException(status_code=404, detail="UI V2 is not enabled")


def _window_state_v2(record: object) -> str:
    value = str(getattr(record, "application_window_status", "") or "").upper()
    return value if value in _WINDOW_STATES else "UNKNOWN"


def _window_label(value: str) -> str:
    return {
        "OPEN": "Open",
        "NOT_YET_OPEN": "Not yet open",
        "CLOSED": "Closed",
        "UNKNOWN": "Unknown",
    }.get(value, "Unknown")


def _local_date(value: object) -> date | None:
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(_LOCAL_TIMEZONE).date()
    return value if isinstance(value, date) else None


def _environment_settings(request: Request):
    original = getattr(request.app.state, "ui_v2_environment_settings", None)
    if original is None:
        original = request.app.state.settings
        request.app.state.ui_v2_environment_settings = original
    return original


def _control_file(request: Request) -> Path:
    settings = _environment_settings(request)
    return Path(settings.data_dir) / "ui-control-v2.json"


def _load_control(request: Request) -> dict[str, object]:
    default_mode = str(
        getattr(getattr(_environment_settings(request), "automation_mode", None), "value", "OFF")
    ).upper()
    state: dict[str, object] = {"automation_mode": default_mode, "dry_run_default": True}
    path = _control_file(request)
    if not path.is_file():
        return state
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return state
    if not isinstance(stored, dict):
        return state
    mode = str(stored.get("automation_mode", "")).upper()
    if mode in {"OFF", "REVIEW_ONLY", "ARMED"}:
        state["automation_mode"] = mode
    if isinstance(stored.get("dry_run_default"), bool):
        state["dry_run_default"] = stored["dry_run_default"]
    return state


def _persist_control(request: Request, state: dict[str, object]) -> None:
    path = _control_file(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)


def _live_submit_environment_authority(request: Request) -> bool:
    original = _environment_settings(request)
    # Keep the restart-bound named flag separate from the effective mode.
    # Runtime changes may narrow live_submit_enabled but can never create this
    # authority. Older embedded Settings objects remain fail-closed.
    return bool(getattr(original, "live_submit_environment_enabled", False))


def _apply_runtime_control(request: Request, state: dict[str, object] | None = None):
    """Apply the persisted UI request to in-process settings.

    This changes no code-level gate: ARMED is capped by the original live-submit
    environment authority, and every object that exposes a settings reference
    receives the same frozen replacement. Existing exact-manifest and egress
    checks remain downstream requirements.
    """

    state = state or _load_control(request)
    original = _environment_settings(request)
    mode_value = str(state.get("automation_mode", "OFF")).upper()
    if mode_value == "ARMED" and not _live_submit_environment_authority(request):
        mode_value = "REVIEW_ONLY"
    try:
        mode = AutomationMode(mode_value)
    except ValueError:
        mode = AutomationMode.OFF
    effective = replace(
        original,
        automation_mode=mode,
        live_submit_enabled=(
            _live_submit_environment_authority(request) and mode.submission_armed
        ),
    )
    request.app.state.settings = effective
    for value in request.app.state._state.values():
        if value is None:
            continue
        for attribute in ("settings", "_settings"):
            if hasattr(value, attribute):
                try:
                    setattr(value, attribute, effective)
                except (AttributeError, TypeError):
                    pass
    return effective


def _status_context(request: Request) -> dict[str, object]:
    control = _load_control(request)
    settings = _apply_runtime_control(request, control)
    mode = str(getattr(settings.automation_mode, "value", settings.automation_mode)).upper()
    mode_label = mode.replace("_", " ")
    may_submit = bool(settings.live_submit_enabled and settings.automation_mode.submission_armed)
    notifications = bool(getattr(settings, "notifications_enabled", False))
    dry_run = bool(control.get("dry_run_default", True))
    sentence = (
        f"Automation: {mode_label} · "
        f"{'submission requests permitted; exact confirmation still required' if may_submit else 'will not submit'} · "
        f"{'dry-run default' if dry_run else 'run mode chosen per action'} · "
        f"notifications {'on' if notifications else 'off'}"
    )
    return {
        "automation_mode": mode,
        "automation_mode_label": mode_label,
        "status_sentence": sentence,
        "dry_run_default": dry_run,
        "notifications_enabled": notifications,
        "live_submit_authority": _live_submit_environment_authority(request),
        "submission_plain": (
            "Possible only after exact typed and manifest confirmation"
            if may_submit
            else "Refused by the current mode"
        ),
    }


def _needs_you_count(session) -> int:
    return len(
        session.scalars(select(Application.id).where(Application.state.in_(_NEEDS_STATES))).all()
    )


def _safe_needs_label(value: object) -> str:
    """Bound field labels and keep candidate values out of the handoff UI."""

    label = " ".join(str(value or "").split())
    label = re.sub(r"\b[^\s@]+@[^\s@]+\b", "[redacted field label]", label)
    label = re.sub(r"(?<!\w)\+?[0-9][0-9 ().-]{6,}[0-9](?!\w)", "[redacted field label]", label)
    return label[:120].rstrip()


def _prefill_run_projection(session, run: AutomationRun) -> dict[str, object]:
    """Project bounded, query-free PREFILL failure evidence into the UI."""

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
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, Mapping):
            details = parsed
    blocked = sanitise_blocked_requests(details.get("blocked_requests", ()))
    return {
        "latest_run_error": " ".join(str(run.error or "").split())[:400].rstrip(),
        "prefill_wall": (
            prefill_wall(
                run_error=run.error,
                blocked_requests=blocked,
                state=run.state,
            )
            if run.mode == "prefill"
            else ""
        ),
        "blocked_egress_hosts": tuple(
            f"{item['host']} ({item['classification']})" for item in blocked
        ),
    }


def _needs_question_projection(session, application_id: str) -> dict[str, object]:
    """Project the latest safe blank-field evidence for Needs You."""

    run = session.scalars(
        select(AutomationRun)
        .where(AutomationRun.application_id == application_id)
        .order_by(AutomationRun.created_at.desc())
    ).first()
    if run is None:
        return {
            "blank_fields": [],
            "blank_field_reasons": [],
            "human_only_fields": [],
            "captcha_required": False,
            "handoff_required": False,
            "latest_run_state": "",
            "handoff_stage": "",
            "latest_run_error": "",
            "prefill_wall": "",
            "blocked_egress_hosts": (),
        }

    records = list(
        session.scalars(
            select(QuestionRecord)
            .where(QuestionRecord.run_id == run.id)
            .order_by(QuestionRecord.id)
        ).all()
    )
    manifest: dict[str, object] = {}
    try:
        parsed_manifest = json.loads(run.receipt_json or "{}")
        if isinstance(parsed_manifest, dict):
            manifest = parsed_manifest
    except (TypeError, ValueError):
        manifest = {}

    manifest_codes: dict[str, str] = {}
    raw_codes = manifest.get("blank_field_reason_codes")
    if isinstance(raw_codes, list):
        for item in raw_codes:
            if not isinstance(item, dict):
                continue
            label = _safe_needs_label(item.get("label"))
            code = str(item.get("reason_code") or "").strip()
            if label and code:
                manifest_codes[label] = code

    option_codes = {"no_matching_option", "ambiguous_option", "commit_failed", "control_unavailable"}
    legal_keys = {"legal.work_authorisation", "legal.sponsorship", "legal.attestation"}
    human_keys = {"handoff.captcha", "handoff.assessment", "sensitive.demographic"}
    details: list[dict[str, str]] = []
    human_only: list[str] = []
    captcha_required = False
    for record in records:
        source = str(record.answer_source or "").strip().casefold()
        status = str(record.mapping_status or "").strip().casefold()
        if source == "option_not_selected":
            continue
        if status == "resolved" and source != "prefill_failed":
            continue
        label = _safe_needs_label(record.label)
        if not label:
            continue
        canonical = str(record.canonical_key or "").strip()
        stored_reason = " ".join(str(record.reason or "").split())[:240].rstrip()
        if source in option_codes:
            reason_code = source
            reason = stored_reason or {
                "no_matching_option": "no matching option was offered by the widget; field left blank",
                "ambiguous_option": "multiple offered options matched; field left blank",
                "commit_failed": "the widget did not commit the offered option; field left blank",
                "control_unavailable": "the combobox was unavailable; field left blank",
            }[source]
        elif source == "prefill_failed":
            reason_code = manifest_codes.get(label, "prefill_failed")
            reason = stored_reason or "approved value could not be committed; field left blank"
        elif canonical in legal_keys or str(record.sensitivity or "").casefold() == "legal":
            reason_code = "legal_answer_missing"
            reason = stored_reason or "no exact stored-profile match; ARGUS will not make a legal declaration"
        elif canonical in human_keys:
            reason_code = "human_handoff_required"
            reason = stored_reason or "requires human completion"
        elif canonical == "unknown" and stored_reason and stored_reason != "No deterministic mapping":
            reason_code = "candidate_answer_required"
            reason = f"{stored_reason}; ARGUS will not invent or infer it"
        elif source == "missing":
            reason_code = "approved_value_missing"
            reason = stored_reason or "no approved stored answer"
        elif source == "unmapped":
            reason_code = "no_approved_mapping"
            reason = "no approved mapping"
        else:
            reason_code = "human_review_required"
            reason = stored_reason or "requires human review"
        item = {"label": label, "reason": reason, "reason_code": reason_code}
        details.append(item)
        if reason_code in {
            "legal_answer_missing",
            "legal_declaration_human_required",
            "candidate_answer_required",
            "human_handoff_required",
            "approved_value_missing",
            "no_approved_mapping",
            "human_review_required",
            "prefill_failed",
        }:
            human_only.append(label)
        if canonical == "handoff.captcha" or "captcha" in label.casefold():
            captcha_required = True

    # A safe manifest can carry reason rows when a run ended before question
    # records were written. It is still label/reason-only evidence.
    if not details:
        raw_reasons = manifest.get("blank_field_reasons")
        if isinstance(raw_reasons, list):
            for item in raw_reasons:
                if not isinstance(item, dict):
                    continue
                label = _safe_needs_label(item.get("label"))
                reason = " ".join(str(item.get("reason") or "").split())[:240].rstrip()
                if label and reason:
                    details.append(
                        {
                            "label": label,
                            "reason": reason,
                            "reason_code": manifest_codes.get(label, "human_review_required"),
                        }
                    )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in details:
        identity = (item["label"], item["reason_code"])
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(item)
    details = deduped[:32]
    human_only = list(dict.fromkeys(human_only))[:32]
    return {
        "blank_fields": [item["label"] for item in details],
        "blank_field_reasons": details,
        "human_only_fields": human_only,
        "captcha_required": captcha_required,
        "handoff_required": bool(details or manifest or run.state in _NEEDS_STATES),
        "latest_run_state": str(run.state or ""),
        "handoff_stage": str(manifest.get("stage") or manifest.get("handoff_stage") or ""),
        **_prefill_run_projection(session, run),
    }


def _base_v2(
    request: Request,
    *,
    title: str,
    active: str,
    session=None,
) -> dict[str, object]:
    needs_count = _needs_you_count(session) if session is not None else 0
    status = _status_context(request)
    posture = {
        "mode": status["automation_mode"],
        "mode_label": status["automation_mode_label"],
        "sentence": status["status_sentence"],
        "dry_run_default": status["dry_run_default"],
        "notifications_enabled": status["notifications_enabled"],
        "live_submit_capability": status["live_submit_authority"],
        "submission_possible": status["submission_plain"].startswith("Possible"),
    }
    return {
        "title": title,
        "active": active,
        "global_query": request.query_params.get("q", "").strip(),
        "needs_you_count": needs_count,
        "needs_count": needs_count,
        "local_timezone": _LOCAL_TIMEZONE_NAME,
        "mode_posture": posture,
        "user_status_options": _USER_STATUS_ORDER,
        **status,
    }


def _opportunity_rows(session) -> list[Opportunity]:
    options = [selectinload(Opportunity.application)]
    if hasattr(Opportunity, "archive_record"):
        options.append(selectinload(Opportunity.archive_record))
    return list(
        session.scalars(
            select(Opportunity)
            .options(*options)
            .order_by(Opportunity.employer.asc(), Opportunity.role_title.asc())
        ).all()
    )


def _candidate_roles_from_evidence(raw: object) -> tuple[SimpleNamespace, ...]:
    """Project only bounded, persisted listing candidates into the UI."""

    try:
        evidence = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(evidence, dict):
        return ()
    raw_candidates = evidence.get("candidate_roles")
    if not isinstance(raw_candidates, (list, tuple)):
        return ()
    candidates: list[SimpleNamespace] = []
    seen: set[str] = set()
    for item in raw_candidates[:50]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:240]
        raw_url = item.get("url")
        if not title or not isinstance(raw_url, str):
            continue
        try:
            url = validate_navigation_url(raw_url)
        except (TypeError, ValueError):
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append(
            SimpleNamespace(
                title=title,
                location=" ".join(str(item.get("location") or "").split())[:240],
                programme_type=" ".join(str(item.get("programme_type") or "").split())[:120],
                url=url,
            )
        )
    return tuple(candidates)


def _project_opportunity(record: Opportunity) -> SimpleNamespace:
    application = getattr(record, "application", None)
    window_state = _window_state_v2(record)
    opening = _local_date(getattr(record, "opening_date", None))
    closing = _local_date(getattr(record, "deadline", None))
    future_dates = [(opening, "Opens"), (closing, "Closes")]
    future_dates = [(value, label) for value, label in future_dates if value is not None]
    next_date, next_kind = min(future_dates, default=(None, ""), key=lambda item: item[0] or date.max)
    archived = bool(getattr(record, "is_archived", False))
    today = datetime.now(_LOCAL_TIMEZONE).date()
    application_state = getattr(application, "state", "NOT_STARTED")
    target_status = str(getattr(record, "target_status", "UNRESOLVED") or "UNRESOLVED")
    user_status = str(
        getattr(record, "user_status", UserApplicationStatus.NOT_APPLIED.value)
        or UserApplicationStatus.NOT_APPLIED.value
    )
    exclusion_reason = user_status_exclusion_reason(user_status)
    candidate_roles = _candidate_roles_from_evidence(
        getattr(record, "resolution_evidence_json", "{}")
    )
    target_choice_required = bool(
        application is not None
        and target_status == TargetKind.MULTIPLE_CANDIDATE_ROLES.value
        and candidate_roles
        and exclusion_reason is None
    )

    def relative(value: date | None) -> str:
        if value is None:
            return "—"
        days = (value - today).days
        if days == 0:
            return "today"
        return f"{abs(days)}d" if days < 0 else f"in {days}d"

    return SimpleNamespace(
        id=record.id,
        employer=record.employer,
        role=record.role_title,
        programme_group=record.programme_group,
        programme=record.programme_group or "unknown",
        division=getattr(record, "division", "") or "General",
        cycle=getattr(record, "cycle", ""),
        location=record.location,
        source_url=record.url,
        window_state=window_state,
        window_label=_window_label(window_state),
        opening_date=opening,
        closing_date=closing,
        opening_relative=relative(opening),
        closing_relative=relative(closing),
        dated=opening is not None or closing is not None,
        next_date=next_date,
        next_date_kind=next_kind,
        application_id=getattr(application, "id", None),
        application_state=application_state,
        application_label=str(application_state).replace("_", " ").title(),
        user_status=user_status,
        user_status_label=user_status.replace("_", " ").title(),
        user_status_actor=str(getattr(record, "user_status_actor", "default") or "default"),
        user_status_updated_at=getattr(record, "user_status_updated_at", None),
        automation_eligible_by_user_status=user_status_is_automation_eligible(user_status),
        automation_exclusion_reason=exclusion_reason,
        is_active_process=user_status in _ACTIVE_PROCESS_USER_STATUSES,
        priority=getattr(application, "priority", 0),
        next_action=getattr(application, "next_action", "") or "Review and evaluate this role",
        archived=archived,
        is_archived=archived,
        archived_reason=getattr(record, "archived_reason", None),
        target=SimpleNamespace(
            status=target_status,
            label=target_status.replace("_", " ").title(),
            candidate_roles=candidate_roles,
            target_choice_required=target_choice_required,
        ),
        candidate_roles=candidate_roles,
        target_choice_required=target_choice_required,
        selected=False,
    )


def _filter_rows(
    rows: list[SimpleNamespace], request: Request
) -> tuple[list[SimpleNamespace], SimpleNamespace]:
    filters = SimpleNamespace(
        q=request.query_params.get("q", "").strip(),
        programme_group=(
            request.query_params.get("programme", "").strip()
            or request.query_params.get("programme_group", "").strip()
        ),
        window_state=request.query_params.get("window_state", "").strip().upper(),
        application_state=request.query_params.get("application_state", "").strip().upper(),
        user_status=request.query_params.get("user_status", "").strip().upper(),
        employer=request.query_params.get("employer", "").strip(),
        role=request.query_params.get("role", "").strip(),
        stage=request.query_params.get("stage", "").strip().casefold(),
    )
    filters.programme = filters.programme_group
    query = filters.q.casefold()
    filtered = []
    for item in rows:
        if query and query not in f"{item.employer} {item.role}".casefold():
            continue
        if filters.programme_group and item.programme_group != filters.programme_group:
            continue
        if filters.window_state and item.window_state != filters.window_state:
            continue
        if filters.application_state and item.application_state != filters.application_state:
            continue
        if filters.user_status and item.user_status != filters.user_status:
            continue
        if filters.stage == "applications" and item.application_id is None:
            continue
        if filters.employer and item.employer != filters.employer:
            continue
        if filters.role and item.id != filters.role:
            continue
        item.selected = bool(filters.role and item.id == filters.role)
        filtered.append(item)
    return filtered, filters


def _bounded_query_int(
    request: Request,
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1_000_000,
) -> int:
    try:
        value = int(request.query_params.get(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _needs_groups(session, navigator=None) -> list[SimpleNamespace]:
    records = session.execute(
        select(Application, Opportunity)
        .join(Opportunity, Application.opportunity_id == Opportunity.id)
        .where(Application.state.in_(_NEEDS_STATES))
        .order_by(Application.priority.desc(), Application.updated_at.desc())
    ).all()
    definitions = [
        ("captcha", "Captcha", "Captcha", "A visible human challenge is waiting.", "No captcha is waiting.", "Review another work group."),
        ("login", "Login required", "Login", "An account or authenticated session is required.", "No login wall is waiting.", "Review another work group."),
        ("question", "Question ARGUS refused to answer", "Answer gap", "A declaration or sensitive field has no approved stored answer.", "No sensitive-answer gap is waiting.", "Add reviewed facts in Library when one appears."),
        ("review", "Review", "Review", "Evidence is ready for an explicit human look.", "No application is awaiting review.", "Review the pipeline or calendar."),
        ("blocked", "Blocked / cannot resolve", "Blocked", "ARGUS could not find or verify a safe application path. No action available yet without target verification.", "No unresolved blocked target is recorded.", "No action is required in this group."),
    ]
    groups = {
        key: SimpleNamespace(
            key=key,
            label=label,
            short_label=short_label,
            description=description,
            fallback=description,
            empty_title=empty_title,
            empty_action=empty_action,
            items=[],
        )
        for key, label, short_label, description, empty_title, empty_action in definitions
    }
    for application, opportunity in records:
        target = str(getattr(opportunity, "target_status", "") or "").upper()
        action = str(application.next_action or "")
        user_status = str(
            getattr(opportunity, "user_status", UserApplicationStatus.NOT_APPLIED.value)
            or UserApplicationStatus.NOT_APPLIED.value
        )
        user_status_excluded = not user_status_is_automation_eligible(user_status)
        candidate_roles = _candidate_roles_from_evidence(
            getattr(opportunity, "resolution_evidence_json", "{}")
        )
        target_choice_required = bool(
            target == TargetKind.MULTIPLE_CANDIDATE_ROLES.value
            and candidate_roles
            and not user_status_excluded
        )
        evidence = f"{target} {action}".casefold()
        handoff = _needs_question_projection(session, application.id)
        handoff["captcha_required"] = bool(
            handoff.get("captcha_required") or target == "HUMAN_CHALLENGE" or "captcha" in evidence
        )
        handoff["handoff_required"] = bool(handoff.get("handoff_required") or handoff["captcha_required"])
        try:
            active_session = (
                navigator.active_for_application(application.id)
                if navigator is not None
                else None
            )
        except Exception:  # noqa: BLE001 - a stale browser must hide controls
            active_session = None
        prefill_start_allowed = bool(
            getattr(opportunity, "application_url", None)
            and prefill_ui_allowed(
                target_status=target,
                user_status=user_status,
                application_state=application.state,
                live_session=active_session is not None,
            )
        )
        prefill_continue_allowed = bool(
            active_session is not None and not user_status_excluded
        )
        live_session_id = (
            str(getattr(active_session, "session_id", ""))
            if active_session is not None
            else ""
        )
        live_session_state = ""
        if active_session is not None:
            live_state = getattr(active_session, "state", "")
            live_session_state = str(getattr(live_state, "value", live_state))
            handoff["handoff_required"] = True
            if not handoff.get("prefill_wall"):
                handoff["prefill_wall"] = HUMAN_BOUNDARY_COPY
        if active_session is not None:
            # A live owner thread is the strongest evidence: keep it in the
            # review bucket while the persisted state catches up.
            key = "review"
        elif target == TargetKind.BLOCKED.value:
            key = "blocked"
        elif target == "HUMAN_CHALLENGE" or "captcha" in evidence:
            key = "captcha"
        elif target == "AUTH_WALL" or "login" in evidence or "authentication" in evidence:
            key = "login"
        elif any(term in evidence for term in ("question", "answer", "declaration", "sensitive", "legal")):
            key = "question"
        elif application.state == "READY_TO_SUBMIT" or "review" in evidence:
            key = "review"
        else:
            key = "blocked"
        groups[key].items.append(
            SimpleNamespace(
                application_id=application.id,
                application_state=application.state,
                employer=opportunity.employer,
                role=opportunity.role_title,
                priority=application.priority,
                next_action=action,
                profile_gap=(action or "No approved stored answer is available."),
                target_status=target,
                target_label=target.replace("_", " ").title(),
                target_resolution_failure=(
                    is_target_resolution_failure(application, opportunity)
                ),
                user_status=user_status,
                user_status_label=user_status.replace("_", " ").title(),
                user_status_excluded=user_status_excluded,
                user_status_exclusion_reason=user_status_exclusion_reason(user_status),
                blank_fields=handoff["blank_fields"],
                blank_field_reasons=handoff["blank_field_reasons"],
                human_only_fields=handoff["human_only_fields"],
                captcha_required=handoff["captcha_required"],
                handoff_required=handoff["handoff_required"],
                latest_run_state=handoff["latest_run_state"],
                handoff_stage=handoff["handoff_stage"],
                live_session_id=live_session_id,
                live_session_state=live_session_state,
                prefill_start_allowed=prefill_start_allowed,
                prefill_continue_allowed=prefill_continue_allowed,
                prefill_wall=handoff["prefill_wall"],
                blocked_egress_hosts=handoff["blocked_egress_hosts"],
                latest_run_error=handoff["latest_run_error"],
                candidate_roles=candidate_roles,
                target_choice_required=target_choice_required,
                stored_url=str(
                    getattr(opportunity, "application_url", None)
                    or getattr(opportunity, "url", "")
                    or ""
                ),
            )
        )
    ordered = [groups[key] for key, *_rest in definitions]
    for group in ordered:
        group.rows = group.items
        group.count = len(group.items)
        group.help = group.description
    return ordered


def _first_value(record: object, *names: str, default: object = "") -> object:
    for name in names:
        value = getattr(record, name, None)
        if value not in (None, ""):
            return value
    return default


def render_today(request: Request):
    with request.app.state.db.session_scope() as session:
        rows = [_project_opportunity(record) for record in _opportunity_rows(session)]
        groups = _needs_groups(session)
        today = datetime.now(_LOCAL_TIMEZONE).date()
        state_counts = {
            state: sum(item.window_state == state for item in rows)
            for state in _WINDOW_STATE_ORDER
        }
        closes = sorted(
            (
                item
                for item in rows
                if item.window_state == "OPEN"
                and item.closing_date is not None
                and today <= item.closing_date <= today + timedelta(days=7)
            ),
            key=lambda item: item.closing_date,
        )
        opens = sorted(
            (
                item
                for item in rows
                if item.window_state == "NOT_YET_OPEN"
                and item.opening_date is not None
                and item.opening_date >= today
            ),
            key=lambda item: item.opening_date,
        )[:8]
        context = {
            **_base_v2(request, title="Today", active="today", session=session),
            "today_label": today.strftime("%A / %d %B %Y").upper(),
            "state_counts": state_counts,
            "closes_this_week": closes,
            "closing_week": closes,
            "opens_next": opens,
            "open_now": [item for item in rows if item.window_state == "OPEN"][:8],
            "undated_count": sum(item.opening_date is None and item.closing_date is None for item in rows),
            "needs_groups": groups,
            "user_status_breakdown": [
                SimpleNamespace(
                    value=status,
                    label=status.replace("_", " "),
                    count=sum(item.user_status == status for item in rows),
                )
                for status in _USER_STATUS_ORDER
            ],
            "active_processes": sorted(
                (item for item in rows if item.is_active_process),
                key=lambda item: (
                    _USER_STATUS_ORDER.index(item.user_status),
                    item.employer.casefold(),
                    item.role.casefold(),
                ),
            ),
        }
        return templates.TemplateResponse(request, "v2/today.html", context)


def _calendar_event(item: SimpleNamespace, value: date, kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        **vars(item),
        row=item,
        date=value,
        kind="Opens" if kind == "opens" else "Closes",
        kind_label="Opening" if kind == "opens" else "Closing deadline",
    )


@router.get("/today", response_class=RedirectResponse)
def today_alias(request: Request) -> RedirectResponse:
    _require_v2(request)
    return RedirectResponse("/", status_code=307)


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    _require_v2(request)
    with request.app.state.db.session_scope() as session:
        all_rows = [_project_opportunity(record) for record in _opportunity_rows(session)]
        rows, filters = _filter_rows(all_rows, request)
        today = datetime.now(_LOCAL_TIMEZONE).date()
        view = request.query_params.get("view", "agenda").strip().casefold()
        if view == "watchlist":
            view = "opens"
        if view not in {"agenda", "month", "opens", "closing"}:
            view = "agenda"
        try:
            selected_day = date.fromisoformat(request.query_params.get("d", ""))
        except ValueError:
            selected_day = today
        events = []
        for item in rows:
            if item.opening_date is not None:
                events.append(_calendar_event(item, item.opening_date, "opens"))
            if item.closing_date is not None:
                events.append(_calendar_event(item, item.closing_date, "closes"))
        events.sort(key=lambda event: (event.date, event.employer.casefold(), event.role.casefold(), event.kind))
        agenda = [event for event in events if event.date >= today]
        opens_soon = [
            _calendar_event(item, item.opening_date, "opens")
            for item in rows
            if item.window_state == "NOT_YET_OPEN"
            and item.opening_date is not None
            and item.opening_date >= today
        ]
        opens_soon.sort(key=lambda event: (event.date, event.employer.casefold()))
        closing_soon = [
            _calendar_event(item, item.closing_date, "closes")
            for item in rows
            if item.window_state == "OPEN" and item.closing_date is not None
        ]
        closing_soon.sort(key=lambda event: (event.date, event.employer.casefold()))
        event_map: dict[date, list[SimpleNamespace]] = {}
        for event in events:
            event_map.setdefault(event.date, []).append(event)
        weeks = []
        for week in month_calendar.Calendar(firstweekday=0).monthdatescalendar(
            selected_day.year, selected_day.month
        ):
            weeks.append(
                [
                    SimpleNamespace(
                        date=day,
                        day=day.day,
                        events=event_map.get(day, []),
                        outside=day.month != selected_day.month,
                        current_month=day.month == selected_day.month,
                        selected=day == selected_day,
                    )
                    for day in week
                ]
            )
        previous_month = (selected_day.replace(day=1) - timedelta(days=1)).replace(day=1)
        next_month = (selected_day.replace(day=28) + timedelta(days=5)).replace(day=1)
        persistent = {
            "q": filters.q,
            "programme": filters.programme,
            "employer": filters.employer,
            "window_state": filters.window_state,
            "user_status": filters.user_status,
        }
        retained = {key: value for key, value in persistent.items() if value}
        for week in weeks:
            for cell in week:
                cell.url = "/calendar?" + urlencode(
                    {"view": "month", "d": cell.date.isoformat(), **retained}
                )
        view_urls = {
            key: "/calendar?" + urlencode({"view": key, **retained})
            for key in ("agenda", "month", "opens", "closing")
        }
        state_urls = {
            state: "/calendar?" + urlencode(
                {"view": view, **retained, "window_state": state}
            )
            for state in _WINDOW_STATE_ORDER
        }
        all_undated = [
            item for item in rows if item.opening_date is None and item.closing_date is None
        ]
        undated_per_page = _bounded_query_int(
            request, "undated_per_page", 100, maximum=200
        )
        undated_total_pages = max(
            1, (len(all_undated) + undated_per_page - 1) // undated_per_page
        )
        undated_page = min(
            _bounded_query_int(request, "undated_page", 1), undated_total_pages
        )
        undated_start = (undated_page - 1) * undated_per_page
        undated_rows = all_undated[undated_start : undated_start + undated_per_page]
        undated_retained = {
            "view": view,
            **retained,
            "undated_per_page": undated_per_page,
        }
        if view == "month":
            undated_retained["d"] = selected_day.isoformat()

        def undated_page_url(destination: int) -> str:
            return "/calendar?" + urlencode(
                {**undated_retained, "undated_page": destination}
            )

        if view == "opens":
            visible_events = opens_soon
        elif view == "closing":
            visible_events = closing_soon
        else:
            visible_events = agenda
        context = {
            **_base_v2(request, title="Calendar", active="calendar", session=session),
            "timezone_name": _LOCAL_TIMEZONE_NAME,
            "view": view,
            "view_urls": view_urls,
            "state_urls": state_urls,
            "filters": filters,
            "query": filters.q,
            "programme": filters.programme,
            "employer": filters.employer,
            "window_state": filters.window_state,
            "user_status": filters.user_status,
            "programme_options": sorted({item.programme_group for item in rows if item.programme_group}),
            "employer_options": sorted({item.employer for item in rows}),
            "state_counts": {
                state: sum(item.window_state == state for item in rows)
                for state in _WINDOW_STATE_ORDER
            },
            "dated_count": sum(item.opening_date is not None or item.closing_date is not None for item in rows),
            "undated": undated_rows,
            "undated_count": len(all_undated),
            "undated_pagination": SimpleNamespace(
                total_pages=undated_total_pages,
                page=undated_page,
                has_previous=undated_page > 1,
                has_next=undated_page < undated_total_pages,
                previous_url=(
                    undated_page_url(undated_page - 1) if undated_page > 1 else ""
                ),
                next_url=(
                    undated_page_url(undated_page + 1)
                    if undated_page < undated_total_pages
                    else ""
                ),
            ),
            "agenda": agenda,
            "events": visible_events,
            "opens_soon": opens_soon,
            "closing_soon": closing_soon,
            "selected_day": selected_day,
            "selected_day_events": event_map.get(selected_day, []),
            "selected_day_rows": event_map.get(selected_day, []),
            "month_label": selected_day.strftime("%B %Y"),
            "month_weeks": weeks,
            "previous_month_url": "/calendar?" + urlencode(
                {"view": "month", "d": previous_month.isoformat(), **retained}
            ),
            "next_month_url": "/calendar?" + urlencode(
                {"view": "month", "d": next_month.isoformat(), **retained}
            ),
        }
        return templates.TemplateResponse(request, "v2/calendar.html", context)


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_page(request: Request):
    _require_v2(request)
    with request.app.state.db.session_scope() as session:
        all_rows = [_project_opportunity(record) for record in _opportunity_rows(session)]
        rows, filters = _filter_rows(all_rows, request)
        requested_page = _bounded_query_int(request, "page", 1)
        per_page = _bounded_query_int(request, "per_page", 100, maximum=200)
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = min(max(requested_page, 1), total_pages)
        start = (page - 1) * per_page
        displayed = rows[start : start + per_page]
        selected = next((item for item in displayed if item.selected), None)
        retained = {
            key: value
            for key, value in {
                "q": filters.q,
                "programme": filters.programme,
                "employer": filters.employer,
                "window_state": filters.window_state,
                "application_state": filters.application_state,
                "user_status": filters.user_status,
                "role": filters.role,
                "stage": filters.stage,
                "per_page": per_page,
            }.items()
            if value not in (None, "")
        }

        def page_url(destination: int) -> str:
            return "/pipeline?" + urlencode({**retained, "page": destination})

        context = {
            **_base_v2(request, title="Pipeline", active="pipeline", session=session),
            "rows": displayed,
            "total_count": len(rows),
            "total": len(rows),
            "selected": selected,
            "filters": filters,
            "programme_options": sorted({item.programme_group for item in rows if item.programme_group}),
            "employer_options": sorted({item.employer for item in rows}),
            "application_options": sorted({item.application_state for item in rows}),
            "application_state_options": sorted({item.application_state for item in rows}),
            "pagination": SimpleNamespace(
                total_pages=total_pages,
                page=page,
                has_previous=page > 1,
                has_next=page < total_pages,
                previous_url=page_url(page - 1) if page > 1 else "",
                next_url=page_url(page + 1) if page < total_pages else "",
            ),
        }
        return templates.TemplateResponse(request, "v2/pipeline.html", context)


@router.post("/pipeline/bulk")
def pipeline_bulk(request: Request, payload: _PipelineBulkPayload) -> JSONResponse:
    """Run only reversible/non-submitting bulk pipeline operations."""

    _require_v2(request)
    identifiers = list(dict.fromkeys(payload.opportunity_ids))
    if not payload.confirmed or payload.expected_count != len(identifiers):
        return JSONResponse(
            status_code=409,
            content={
                "code": "exact_count_confirmation_required",
                "message": (
                    f"Confirm the exact {len(identifiers)} selected rows before "
                    "running this action."
                ),
                "submitted": False,
            },
        )

    affected = 0
    try:
        with request.app.state.db.session_scope() as session:
            records = list(
                session.scalars(
                    select(Opportunity)
                    .options(
                        selectinload(Opportunity.application),
                        selectinload(Opportunity.archive_record),
                    )
                    .where(Opportunity.id.in_(identifiers))
                ).all()
            )
            if len(records) != len(identifiers):
                raise HTTPException(
                    status_code=404,
                    detail="One or more selected roles no longer exist",
                )

            if payload.action == "requeue":
                service = ApplicationService(
                    session,
                    request.app.state.settings,
                    request.app.state.crypto,
                )
                for record in records:
                    if record.application is None:
                        raise ApplicationBlockedError(
                            f"{record.employer} has no application record to requeue"
                        )
                    service.queue(record.application.id)
                    affected += 1
            else:
                now = datetime.now(timezone.utc)
                for record in records:
                    archive = record.archive_record
                    if payload.action == "archive":
                        if archive is None:
                            archive = OpportunityArchive(opportunity_id=record.id)
                            session.add(archive)
                        if archive.archived_at is None:
                            archive.archived_at = now
                            archive.archived_reason = "manual_ui_bulk"
                            append_audit(
                                session,
                                AuditInput(
                                    "user",
                                    "opportunity.archived",
                                    "opportunity",
                                    record.id,
                                    {"reason": "manual_ui_bulk", "submitted": False},
                                ),
                            )
                            affected += 1
                    elif (
                        archive is not None
                        and archive.archived_at is not None
                        and archive.archived_reason == "manual_ui_bulk"
                    ):
                        archive.archived_at = None
                        archive.archived_reason = None
                        append_audit(
                            session,
                            AuditInput(
                                "user",
                                "opportunity.unarchived",
                                "opportunity",
                                record.id,
                                {"reason": "manual_ui_bulk", "submitted": False},
                            ),
                        )
                        affected += 1
    except ApplicationBlockedError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "code": "bulk_action_blocked",
                "message": str(exc),
                "submitted": False,
            },
        )

    return JSONResponse(
        content={
            "action": payload.action,
            "selected": len(identifiers),
            "affected": affected,
            "submitted": False,
        }
    )


@router.get("/pipeline/{opportunity_id}", response_class=HTMLResponse)
def pipeline_detail(request: Request, opportunity_id: str):
    _require_v2(request)
    with request.app.state.db.session_scope() as session:
        record = session.scalar(
            select(Opportunity)
            .options(
                selectinload(Opportunity.application),
                selectinload(Opportunity.archive_record),
            )
            .where(Opportunity.id == opportunity_id)
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        row = _project_opportunity(record)
        return templates.TemplateResponse(
            request,
            "v2/pipeline_detail.html",
            {
                **_base_v2(
                    request,
                    title="Pipeline detail",
                    active="pipeline",
                    session=session,
                ),
                "row": row,
            },
        )


def render_needs_you(request: Request):
    with request.app.state.db.session_scope() as session:
        groups = _needs_groups(
            session,
            navigator=getattr(request.app.state, "navigator", None),
        )
        requested_group = request.query_params.get("group", "").strip().casefold()
        selected_group = next(
            (group for group in groups if group.key == requested_group), None
        )
        if selected_group is None:
            selected_group = next((group for group in groups if group.count), groups[3])
        context = {
            **_base_v2(request, title="Needs You", active="needs-you", session=session),
            "groups": groups,
            "selected_group": selected_group,
            "total": sum(group.count for group in groups),
        }
        return templates.TemplateResponse(request, "v2/needs_you.html", context)


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    _require_v2(request)
    tab = request.query_params.get("tab", "profile").strip().casefold()
    if tab not in {"profile", "answers", "documents"}:
        tab = "profile"
    with request.app.state.db.session_scope() as session:
        crypto = request.app.state.crypto
        profile = ProfileService(session, crypto).public_dict()
        answer_rows = AnswerService(session, crypto).list_public()
        context = {
            **_base_v2(request, title="Library", active="library", session=session),
            "tab": tab,
            "profile": profile,
            "answers": answer_rows,
            "documents": list(session.scalars(select(Document).order_by(Document.created_at.desc())).all()),
        }
        return templates.TemplateResponse(request, "v2/library.html", context)


@router.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    _require_v2(request)
    tab = request.query_params.get("tab", "audit").strip().casefold()
    if tab not in {"audit", "mail"}:
        tab = "audit"
    with request.app.state.db.session_scope() as session:
        mail_order = next(
            (
                value
                for value in (
                    getattr(EmailMessage, "received_at", None),
                    getattr(EmailMessage, "created_at", None),
                    getattr(EmailMessage, "id", None),
                )
                if value is not None
            ),
            None,
        )
        mail_statement = select(EmailMessage)
        if mail_order is not None:
            mail_statement = mail_statement.order_by(mail_order.desc())
        mail_records = list(session.scalars(mail_statement.limit(250)).all())
        mail_rows = [
            SimpleNamespace(
                received_at=_first_value(item, "received_at", "created_at"),
                sender=_first_value(item, "sender", "from_address", "sender_email"),
                subject=_first_value(item, "subject", default="No subject captured"),
                classification=_first_value(
                    item, "classification", "message_type", "status", default="Unclassified"
                ),
            )
            for item in mail_records
        ]
        context = {
            **_base_v2(request, title="Activity", active="activity", session=session),
            "tab": tab,
            "audits": list(
                session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(250)).all()
            ),
            "mail": mail_rows,
        }
        return templates.TemplateResponse(request, "v2/activity.html", context)


def _plain_value(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None or value == "":
        return "not set"
    return str(value)


@router.get("/control", response_class=HTMLResponse)
def control_page(request: Request):
    _require_v2(request)
    with request.app.state.db.session_scope() as session:
        settings = _environment_settings(request)
        locked = [
            ("ARGUS_ENABLE_LIVE_SUBMIT", "Live submit authority", _live_submit_environment_authority(request), "Hard environment authority required before ARMED can permit a submission request."),
            ("ARGUS_ENABLE_TRACKR_LIVE", "Live Trackr access", getattr(settings, "trackr_live_enabled", False), "Permits explicitly opted-in live Trackr collection and its network cost."),
            ("ARGUS_ENABLE_APPLY_CLICK", "Guarded Apply click", getattr(settings, "apply_click_enabled", False), "Allows guarded application-entry clicks; it is not submission authority."),
            ("ARGUS_ENABLE_ROLE_MATCH_V2", "Role match V2", getattr(settings, "role_match_v2_enabled", False), "Enables the newer role-identity matching path."),
            ("ARGUS_ENABLE_NOTIFICATIONS", "Notifications", getattr(settings, "notifications_enabled", False), "Emits configured local/Hermes notifications and may invoke the configured transport."),
            ("ARGUS_HERMES_NOTIFY_TARGET", "Hermes target", getattr(settings, "hermes_notify_target", None), "Names the restart-bound Hermes destination; never edited in the browser."),
            ("ARGUS_BROWSER_HEADLESS", "Headless browser", getattr(settings, "browser_headless", True), "Chooses hidden or visible browser execution at process startup."),
            ("ARGUS_AUTOPILOT_MODE", "Automation environment ceiling", getattr(getattr(settings, "automation_mode", None), "value", "OFF"), "Sets the restart-time automation authority ceiling."),
            ("ARGUS_SWEEP_INTERVAL_HOURS", "Sweep interval", getattr(settings, "sweep_interval_hours", 0), "Controls scheduled sweep frequency and its compute/network cost."),
            ("ARGUS_APPLY_CLICK_RUN_CAP", "Apply-click run cap", getattr(settings, "apply_click_run_cap", 0), "Caps guarded application-entry clicks per run."),
        ]
        environment_settings = [
            SimpleNamespace(name=name, label=label, value=_plain_value(value), description=description)
            for name, label, value, description in locked
        ]
        conflict_rules = list(
            session.scalars(
                select(ConflictRule).order_by(ConflictRule.employer_pattern, ConflictRule.cycle)
            ).all()
        )
        context = {
            **_base_v2(request, title="Control", active="control", session=session),
            "environment_settings": environment_settings,
            "environment_flags": environment_settings,
            "conflict_rules": conflict_rules,
            "settings": settings,
        }
        return templates.TemplateResponse(request, "v2/control.html", context)


@router.post("/control/mode")
async def set_control_mode(request: Request):
    _require_v2(request)
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    mode = str(payload.get("mode", "")).upper()
    if mode not in {"OFF", "REVIEW_ONLY", "ARMED"}:
        return JSONResponse(
            {"code": "invalid_mode", "message": "Choose OFF, REVIEW_ONLY, or ARMED.", "submitted": False},
            status_code=422,
        )
    if mode == "ARMED":
        if not _live_submit_environment_authority(request):
            return JSONResponse(
                {
                    "code": "live_submit_environment_locked",
                    "message": "ARMED was refused. Set ARGUS_ENABLE_LIVE_SUBMIT=true in the environment and restart ARGUS first.",
                    "submitted": False,
                },
                status_code=409,
            )
        if str(payload.get("confirmation", "")) != "ARM ARGUS":
            return JSONResponse(
                {
                    "code": "typed_confirmation_required",
                    "message": "Type ARM ARGUS exactly. Nothing changed.",
                    "submitted": False,
                },
                status_code=409,
            )
    state = {
        "automation_mode": mode,
        "dry_run_default": bool(payload.get("dry_run_default", True)),
    }
    _persist_control(request, state)
    effective = _apply_runtime_control(request, state)
    request.app.state.ui_v2_runtime = dict(state)
    return {
        "mode": effective.automation_mode.value,
        "dry_run_default": state["dry_run_default"],
        "message": f"Automation mode is now {effective.automation_mode.value.replace('_', ' ')}. No submission was made.",
        "submitted": False,
    }


def _with_user_status_application_controls(
    context: dict[str, object],
) -> dict[str, object]:
    """Project candidate-owned status and fail closed on rendered controls."""

    projected = dict(context)
    opportunity = projected.get("opportunity")
    status = str(
        getattr(opportunity, "user_status", UserApplicationStatus.NOT_APPLIED.value)
        or UserApplicationStatus.NOT_APPLIED.value
    )
    reason = user_status_exclusion_reason(status)
    projected.update(
        user_status=status,
        user_status_label=status.replace("_", " ").title(),
        automation_exclusion_reason=reason,
    )
    if reason:
        safe_actions = dict(projected.get("safe_actions") or {})
        for key in tuple(safe_actions):
            if key.startswith("can_"):
                safe_actions[key] = False
        projected["safe_actions"] = safe_actions
        target = dict(projected.get("target") or {})
        target["navigator_allowed"] = False
        projected["target"] = target
    return projected


def _with_prefill_application_controls(
    context: dict[str, object],
    request: Request,
    *,
    session=None,
) -> dict[str, object]:
    """Project one truthful candidate-started PREFILL control set."""

    projected = dict(context)
    application = projected.get("application")
    opportunity = projected.get("opportunity")
    target = dict(projected.get("target") or {})
    application_state = str(getattr(application, "state", "") or "").upper()
    target_status = str(
        target.get("status")
        or getattr(opportunity, "target_status", "UNRESOLVED")
        or "UNRESOLVED"
    ).upper()
    user_status = str(
        projected.get("user_status")
        or getattr(opportunity, "user_status", UserApplicationStatus.NOT_APPLIED.value)
        or UserApplicationStatus.NOT_APPLIED.value
    )
    excluded = user_status_exclusion_reason(user_status) is not None
    navigator = getattr(request.app.state, "navigator", None)
    try:
        active_session = (
            navigator.active_for_application(str(getattr(application, "id", "")))
            if navigator is not None and application is not None
            else None
        )
    except Exception:  # noqa: BLE001 - never render a speculative Continue
        active_session = None
    prefill_start_allowed = bool(
        getattr(opportunity, "application_url", None)
        and bool(target.get("automation_eligible"))
        and target_status == PREFILL_TARGET_STATUS
        and prefill_ui_allowed(
            target_status=target_status,
            user_status=user_status,
            application_state=application_state,
            live_session=active_session is not None,
        )
    )
    prefill_continue_allowed = bool(active_session is not None and not excluded)
    safe_actions = dict(projected.get("safe_actions") or {})
    safe_actions["can_prefill"] = prefill_start_allowed
    projected["safe_actions"] = safe_actions
    if excluded:
        prefill_start_allowed = False
        prefill_continue_allowed = False

    wall = str(projected.get("prefill_wall") or "")
    handoff = dict(projected.get("handoff") or {})
    if session is not None:
        handoff = _needs_question_projection(
            session,
            str(getattr(application, "id", "")),
        ) if application is not None else handoff
        runs = projected.get("runs") or ()
        latest_prefill = next(
            (
                run
                for run in runs
                if getattr(run, "mode", "") == RunMode.PREFILL.value
            ),
            None,
        )
        if latest_prefill is not None:
            wall = str(_prefill_run_projection(session, latest_prefill).get("prefill_wall") or wall)
    if active_session is not None and not wall:
        wall = HUMAN_BOUNDARY_COPY
    projected.update(
        target=target,
        active_navigator_session=active_session,
        prefill_start_allowed=prefill_start_allowed,
        prefill_continue_allowed=prefill_continue_allowed,
        prefill_wall=wall,
        handoff=handoff,
        human_boundary_copy=HUMAN_BOUNDARY_COPY,
    )
    return projected


def render_application_detail(request: Request, context: dict[str, object]):
    return templates.TemplateResponse(
        request,
        "v2/application_detail.html",
        {
            **_base_v2(request, title="Application", active="pipeline"),
            "legacy_workflow_js": True,
            **_with_prefill_application_controls(
                _with_user_status_application_controls(context),
                request,
            ),
        },
    )


def render_application_detail_by_id(request: Request, application_id: str):
    # Imported lazily to keep the V1 router as the owner of its mature evidence
    # projection while avoiding a module-import cycle.
    from app.routers.pages import _application_audit_filter, _application_detail_context

    with request.app.state.db.session_scope() as session:
        application = session.get(Application, application_id)
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        # Resolve every relationship used by the projection while the session
        # is open; the V2 template receives no new authority or destination.
        _ = application.opportunity
        _ = application.selected_cv
        _ = application.selected_cover_letter
        runs = list(
            session.scalars(
                select(AutomationRun)
                .where(AutomationRun.application_id == application_id)
                .order_by(AutomationRun.created_at.desc())
            ).all()
        )
        audits = list(
            session.scalars(
                select(AuditEvent)
                .where(_application_audit_filter(application_id, runs))
                .order_by(AuditEvent.id.desc())
            ).all()
        )
        context = _with_prefill_application_controls(
            _with_user_status_application_controls(
                _application_detail_context(application, runs, audits, request)
            ),
            request,
            session=session,
        )
        return templates.TemplateResponse(
            request,
            "v2/application_detail.html",
            {
                **_base_v2(request, title="Application", active="pipeline", session=session),
                "legacy_workflow_js": True,
                **context,
            },
        )
