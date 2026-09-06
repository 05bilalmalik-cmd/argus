from __future__ import annotations

import json
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app.automation.host_policy import (
    BlockedRequestImpact,
    classify_blocked_request_impact,
)
from app.automation.types import RunMode
from app.domain.states import user_status_exclusion_reason
from app.domain.targets import TargetKind
from app.models import (
    Application,
    AuditEvent,
    AutomationRun,
    ConflictRule,
    Document,
    EmailMessage,
    Opportunity,
)
from app.services.answers import AnswerService
from app.services.applications import ApplicationService
from app.services.dashboard import DashboardService
from app.services.opportunities import OpportunityService
from app.services.profile import ProfileService
from app.routers import pages_v2

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
router.include_router(pages_v2.router)

# Compatibility alias for read-only application-window projections.
_window_state_v2 = pages_v2._window_state_v2

_OPPORTUNITY_REVIEW_COOKIE = "argus_opportunities_reviewed_at"
_APPLICATION_REVIEW_COOKIE = "argus_applications_reviewed_at"
_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 100
_POST_SUBMISSION_STATES = frozenset(
    {
        "SUBMITTED",
        "CONFIRMATION_VERIFIED",
        "OA_PENDING",
        "INTERVIEW",
        "REJECTED",
        "OFFER",
    }
)

_TARGET_LABELS = {
    TargetKind.UNRESOLVED.value: "Unresolved target",
    TargetKind.LISTING.value: "Listing / not an application form",
    TargetKind.MULTIPLE_CANDIDATE_ROLES.value: "Multiple roles — human choice required",
    TargetKind.JOB_DETAIL.value: "Job detail / application entry not verified",
    TargetKind.APPLICATION_ENTRY.value: "Verified application entry",
    TargetKind.APPLICATION_FORM.value: "Verified application form",
    TargetKind.AUTH_WALL.value: "Authentication wall",
    TargetKind.HUMAN_CHALLENGE.value: "Human challenge",
    TargetKind.NON_HTML.value: "Non-HTML target",
    TargetKind.MISMATCH.value: "Employer or role mismatch",
    TargetKind.BLOCKED.value: "Target blocked",
}
_TARGET_UNVERIFIED_LABELS = {
    TargetKind.APPLICATION_ENTRY.value: "Application entry not verified",
    TargetKind.APPLICATION_FORM.value: "Application form not verified",
}
_TARGET_AUTOMATION_STATES = frozenset(
    {TargetKind.APPLICATION_ENTRY.value, TargetKind.APPLICATION_FORM.value}
)
_NO_NAVIGATOR_APPLICATION_STATES = frozenset(
    {
        "DISCOVERED",
        "NEEDS_OA",
        "SUBMISSION_UNKNOWN",
        "BLOCKED",
        "SUBMITTED",
        "CONFIRMATION_VERIFIED",
        "OA_PENDING",
        "INTERVIEW",
        "REJECTED",
        "OFFER",
    }
)


def _base(request: Request, *, title: str, active: str) -> dict[str, object]:
    """Build shared chrome data even when a lightweight test settings object is used."""

    settings = getattr(request.app.state, "settings", None)
    live_submit = bool(getattr(settings, "live_submit_enabled", False))
    allowlist = sorted(getattr(settings, "live_domain_allowlist", ()) or ())
    configured_mode = getattr(settings, "automation_mode", None)
    mode_value = getattr(configured_mode, "value", configured_mode)
    mode_value = str(mode_value).upper() if mode_value else ""
    mode_labels = {
        "OFF": ("OFF", "Automation is disabled; no actions will run."),
        "REVIEW_ONLY": ("REVIEW ONLY", "Automation can prepare evidence but waits for review."),
        "ARMED": ("ARMED", "Automation is armed; guardrails still apply."),
        "RUNNING": ("RUNNING", "An automation run is currently in progress."),
    }
    mode_label, mode_description = mode_labels.get(
        mode_value,
        (
            "LIVE SUBMIT ENABLED" if live_submit else "DRY-RUN DEFAULT",
            "Live submission is explicitly enabled; guardrails still apply."
            if live_submit
            else "No live submission is enabled. Actions stay in dry-run mode.",
        ),
    )
    return {
        "title": title,
        "active": active,
        "live_submit": live_submit,
        "allowlist": allowlist,
        "mode_label": mode_label,
        "mode_description": mode_description,
        "automation_mode": mode_value or None,
        "settings_available": settings is not None,
    }


def _positive_int(request: Request, name: str, default: int, maximum: int) -> int:
    try:
        value = int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(1, value))


def _reviewed_at(request: Request, cookie_name: str) -> datetime | None:
    raw = request.cookies.get(cookie_name)
    if not raw:
        return None
    try:
        marker = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if marker.tzinfo is None:
        marker = marker.replace(tzinfo=timezone.utc)
    return marker.astimezone(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_new(first_seen: datetime | None, reviewed_at: datetime | None) -> bool:
    first_seen = _utc(first_seen)
    return first_seen is not None and (reviewed_at is None or first_seen > reviewed_at)


def _review_label(reviewed_at: datetime | None) -> str:
    return (
        reviewed_at.astimezone().strftime("%d %b %Y, %H:%M")
        if reviewed_at
        else "Never"
    )


def _query_url(path: str, params: dict[str, str], *, page: int | None = None) -> str:
    values = dict(params)
    if page is not None:
        values["page"] = str(page)
    query = urlencode({key: value for key, value in values.items() if value != ""})
    return f"{path}?{query}" if query else path


def _pagination(
    path: str,
    *,
    page: int,
    per_page: int,
    total: int,
    params: dict[str, str],
) -> dict[str, object]:
    total_pages = max(1, ceil(total / per_page))
    page = min(max(1, page), total_pages)
    start = 0 if total == 0 else ((page - 1) * per_page) + 1
    end = min(page * per_page, total) if total else 0
    pages = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "start": start,
        "end": end,
        "pages": pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "previous_url": _query_url(path, params, page=page - 1) if page > 1 else "",
        "next_url": _query_url(path, params, page=page + 1) if page < total_pages else "",
        "page_url": lambda target: _query_url(path, params, page=target),
    }


def _page_params(request: Request, names: tuple[str, ...]) -> dict[str, str]:
    return {name: request.query_params.get(name, "").strip() for name in names}


def _json_value(raw: str, fallback: object) -> object:
    try:
        return json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _application_audit_filter(
    application_id: str,
    runs: list[AutomationRun],
):
    application_events = and_(
        AuditEvent.entity_type == "application",
        AuditEvent.entity_id == application_id,
    )
    run_ids = [str(run.id) for run in runs]
    if not run_ids:
        return application_events
    return or_(
        application_events,
        and_(
            AuditEvent.entity_type == "automation_run",
            AuditEvent.entity_id.in_(run_ids),
        ),
    )


def _measured_first_party_path(
    path: str,
    *,
    resource_type: str,
    candidate_data_kind: str,
) -> bool:
    """Match only paths measured for the canonical manifest endpoint type."""

    if candidate_data_kind == "email":
        return path == "/address/validate"
    if candidate_data_kind == "location":
        return path == "/v1/autocomplete"
    if resource_type == "fetch":
        return path == "/users" + "/self"
    if resource_type == "image":
        return path == "/assets/flags-a2kmUSbF.webp"
    return False


def _first_party_disclosure_record(
    raw: object,
    *,
    current_page_urls: tuple[str, ...],
    expected_vendor: str,
) -> dict[str, object] | None:
    """Independently re-authorize one record before candidate-facing rendering."""

    if not isinstance(raw, Mapping):
        return None
    manifest_permission = raw.get("manifest_permission")
    carries_candidate_data = raw.get("carries_candidate_data")
    if manifest_permission is not True or type(carries_candidate_data) is not bool:
        return None

    string_fields = {
        key: raw.get(key)
        for key in (
            "host",
            "path",
            "method",
            "resource_type",
            "manifest_vendor",
            "delivery",
            "candidate_data_kind",
        )
    }
    if not all(isinstance(item, str) for item in string_fields.values()):
        return None
    host = string_fields["host"]
    path = string_fields["path"]
    method = string_fields["method"]
    resource_type = string_fields["resource_type"]
    manifest_vendor = string_fields["manifest_vendor"]
    delivery = string_fields["delivery"]
    candidate_data_kind = string_fields["candidate_data_kind"]

    if method not in {"GET", "HEAD"} or delivery != "blocked":
        return None
    if not expected_vendor or manifest_vendor != expected_vendor:
        return None
    if not _measured_first_party_path(
        path,
        resource_type=resource_type,
        candidate_data_kind=candidate_data_kind,
    ):
        return None
    if not current_page_urls:
        return None
    for current_page_url in current_page_urls:
        decision = classify_blocked_request_impact(
            url=f"https://{host}{path}",
            method=method,
            resource_type=resource_type,
            is_navigation_request=False,
            mode=RunMode.PREFILL.value,
            policy_classification=(
                "data_bearing" if carries_candidate_data else "other"
            ),
            resolved_vendor=manifest_vendor,
            current_page_url=current_page_url,
        )
        if decision.impact is not BlockedRequestImpact.FIRST_PARTY_SERVICE:
            return None
        if decision.carries_candidate_data is not carries_candidate_data:
            return None
        if (decision.candidate_data_kind or "") != candidate_data_kind:
            return None

    if carries_candidate_data and candidate_data_kind not in {"email", "location"}:
        return None
    if not carries_candidate_data and candidate_data_kind:
        return None

    return {
        "host": host,
        "path": path,
        "method": method,
        "resource_type": resource_type,
        "manifest_vendor": manifest_vendor,
        "manifest_permission": True,
        "delivery": "blocked",
        "carries_candidate_data": carries_candidate_data,
        "candidate_data_kind": candidate_data_kind,
    }


def _target_ui(record: Opportunity, application: Application | None = None) -> dict[str, object]:
    """Build safe, evidence-derived target labels for page templates.

    The source URL is intentionally returned only as ``source_url``.  A
    verified application destination is represented as metadata for the local
    navigator; browser JavaScript never treats it as a link or raw navigation
    target.
    """

    raw_status = str(getattr(record, "target_status", "") or TargetKind.UNRESOLVED.value)
    status = raw_status.upper()
    try:
        kind = TargetKind(status)
    except ValueError:
        kind = TargetKind.UNRESOLVED
        status = kind.value
    evidence = _json_value(getattr(record, "resolution_evidence_json", "{}"), {})
    candidate_roles = pages_v2._candidate_roles_from_evidence(
        getattr(record, "resolution_evidence_json", "{}")
    )
    user_status = str(getattr(record, "user_status", "NOT_APPLIED") or "NOT_APPLIED")
    application_state = str(getattr(application, "state", "") or "")
    target_choice_required = bool(
        status == TargetKind.MULTIPLE_CANDIDATE_ROLES.value
        and candidate_roles
        and application is not None
        and user_status_exclusion_reason(user_status) is None
        and application_state in {"DISCOVERED", "NEEDS_USER", "BLOCKED", "FAILED_RETRYABLE"}
    )
    reasons: list[str] = []
    if isinstance(evidence, dict):
        raw_reasons = evidence.get("reason_codes") or evidence.get("reasons") or ()
        if isinstance(raw_reasons, (list, tuple, set)):
            reasons.extend(str(value) for value in raw_reasons if str(value).strip())
        elif raw_reasons:
            reasons.append(str(raw_reasons))
    if not reasons and not kind.automation_eligible:
        reasons.append(status.casefold())
    app_state = str(getattr(application, "state", "") or "")
    # ``target_status`` is historical metadata and can outlive the verified
    # destination that originally justified it.  Only expose a verified label
    # while the model can produce a current automation URL *and* has a
    # resolution timestamp for that promotion.  This keeps stale
    # APPLICATION_ENTRY/FORM rows visibly actionable instead of implying that
    # the Navigator may safely resume them.
    automation_eligible = (
        kind in _TARGET_AUTOMATION_STATES
        and bool(getattr(record, "automation_url", None))
        and getattr(record, "resolved_at", None) is not None
    )
    navigator_allowed = automation_eligible and app_state not in _NO_NAVIGATOR_APPLICATION_STATES
    provider = str(getattr(record, "resolved_ats_type", "") or "")
    if not provider:
        provider = "unknown / unverified"
    return {
        "status": status,
        "label": (
            _TARGET_LABELS.get(status, status.replace("_", " ").title())
            if automation_eligible or status not in _TARGET_UNVERIFIED_LABELS
            else _TARGET_UNVERIFIED_LABELS[status]
        ),
        "reasons": tuple(dict.fromkeys(reasons)),
        "automation_eligible": automation_eligible,
        "navigator_allowed": navigator_allowed,
        "application_id": getattr(application, "id", "") if application else "",
        "application_state": app_state,
        "provider": provider,
        "source_url": str(getattr(record, "url", "") or ""),
        "destination": str(getattr(record, "automation_url", "") or ""),
        "candidate_roles": candidate_roles,
        "target_choice_required": target_choice_required,
    }


def _application_detail_context(
    application: Application,
    runs: list[AutomationRun],
    audits: list[AuditEvent],
    request: Request,
) -> dict[str, object]:
    eligibility = _json_value(application.eligibility_json, {})
    conflict = _json_value(application.conflict_json, {})
    blockers: list[str] = []
    if isinstance(eligibility, dict):
        for reason in eligibility.get("reason_codes", ()) or ():
            blockers.append(str(reason))
        if eligibility.get("eligible") is False and not blockers:
            blockers.append("eligibility_not_confirmed")
    if isinstance(conflict, dict):
        for reason in conflict.get("reason_codes", ()) or ():
            blockers.append(str(reason))
        if conflict.get("blocked") is True and not blockers:
            blockers.append("employer_conflict")
    if application.state in {"NEEDS_USER", "NEEDS_OA", "BLOCKED", "FAILED_RETRYABLE"}:
        if application.next_action:
            blockers.append(application.next_action)
    # A run failure is actionable while the application is still in the
    # guarded submission workflow.  Once the application has moved beyond
    # submission, retain the failure in run history but do not misrepresent
    # it as a current blocker for the next action.
    if application.state not in _POST_SUBMISSION_STATES:
        for run in runs:
            if run.error:
                blockers.append(run.error)

    artifacts: list[dict[str, object]] = []
    for label, record in (
        ("CV", application.selected_cv),
        ("Cover letter", application.selected_cover_letter),
    ):
        if record is not None:
            artifacts.append(
                {
                    "label": label,
                    "name": record.name,
                    "path": record.path,
                    "approved": record.approved,
                }
            )
    for run in runs:
        for label, path in (("Trace", run.trace_path), ("Screenshot", run.screenshot_path)):
            if path:
                artifacts.append(
                    {
                        "label": label,
                        "name": Path(path).name,
                        "path": path,
                        "approved": None,
                    }
                )

    audit_history: list[dict[str, object]] = []
    first_party_requests: list[dict[str, object]] = []
    runs_by_id = {str(run.id): run for run in runs}
    current_application_url = str(application.opportunity.application_url or "")
    current_vendor = str(application.opportunity.resolved_ats_type or "")
    for event in audits:
        details = _json_value(event.details_json, {})
        if isinstance(details, Mapping):
            history_details = dict(details)
            raw_first_party = history_details.pop("first_party_requests", ())
            event_run = runs_by_id.get(str(event.entity_id))
            audit_application_url = history_details.get("application_url")
            if (
                event.event_type == "automation.run_finished"
                and event.entity_type == "automation_run"
                and event_run is not None
                and event_run.mode == RunMode.PREFILL.value
                and event_run.adapter == current_vendor
                and history_details.get("application_id") == application.id
                and history_details.get("mode") == RunMode.PREFILL.value
                and isinstance(audit_application_url, str)
                and isinstance(raw_first_party, (list, tuple))
            ):
                for raw in raw_first_party:
                    disclosure = _first_party_disclosure_record(
                        raw,
                        current_page_urls=(
                            current_application_url,
                            audit_application_url,
                        ),
                        expected_vendor=current_vendor,
                    )
                    if disclosure is not None:
                        first_party_requests.append(disclosure)
        else:
            history_details = details
        audit_history.append({"record": event, "details": history_details})
    target = _target_ui(application.opportunity, application)
    answer_manifest: list[dict[str, str]] = []
    for run in runs:
        for question in getattr(run, "questions", ()):
            answer_manifest.append(
                {
                    "label": str(question.label or ""),
                    "status": str(question.mapping_status or "unmapped"),
                    "source": str(question.answer_source or ""),
                }
            )
    document_manifest = [
        {
            "kind": label,
            "name": record.name,
            "approved": "approved" if record.approved else "not approved",
        }
        for label, record in (
            ("CV", application.selected_cv),
            ("Cover letter", application.selected_cover_letter),
        )
        if record is not None
    ]
    navigator_manifest = {
        "application_id": application.id,
        "employer": application.opportunity.employer,
        "role": application.opportunity.role_title,
        "provider": target["provider"],
        "destination": target["destination"],
        "documents": document_manifest,
        "answers": answer_manifest,
    }
    safe_actions = {
        "can_queue": application.state
        in {"ELIGIBILITY_CHECKED", "NEEDS_USER", "FAILED_RETRYABLE"},
        "can_prepare": application.state in {"QUEUED", "NEEDS_USER"},
        "can_dry_run": application.state in {"PACKAGE_PREPARED", "FILLING"},
        # READY_TO_SUBMIT is resumed through the Navigator's exact manifest;
        # the page never calls the legacy runner submit endpoint directly.
        "can_navigate": bool(target["navigator_allowed"]),
        "can_review_manifest": application.state == "READY_TO_SUBMIT"
        and bool(target["navigator_allowed"]),
        # Target resolution is an explicit, application-bound action.  It
        # opens the visible Navigator/human workflow and never submits.
        "can_resolve_target": (
            not bool(target["automation_eligible"])
            and application.state not in _POST_SUBMISSION_STATES
            and application.state not in {"NEEDS_OA", "SUBMISSION_UNKNOWN"}
        ),
        # Retain this state marker for compatibility with detail consumers,
        # but the UI must use the human-bound blocker-resolution workflow;
        # BLOCKED is never sent directly to the generic queue endpoint.
        "can_requeue": application.state == "BLOCKED",
        # NEEDS_OA has no resumable automation path; say so instead of
        # offering controls the runner would refuse.
        "is_needs_oa": application.state == "NEEDS_OA",
        # PREFILL: explicit user-initiated form fill on a visible browser;
        # never submits — the human performs every final click themselves.
        "can_prefill": application.state
        in {"PACKAGE_PREPARED", "FILLING", "NEEDS_USER", "FAILED_RETRYABLE"},
    }
    return {
        "application": application,
        "opportunity": application.opportunity,
        "runs": runs,
        "audits": audit_history,
        "first_party_requests": first_party_requests,
        "eligibility": eligibility,
        "conflict": conflict,
        "target": target,
        "navigator_manifest": navigator_manifest,
        "blockers": tuple(dict.fromkeys(blockers)),
        "artifacts": artifacts,
        "safe_actions": safe_actions,
        "last_reviewed": _review_label(_reviewed_at(request, _APPLICATION_REVIEW_COOKIE)),
    }


@router.get("/", response_class=HTMLResponse)
def command_centre(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return pages_v2.render_today(request)
    with request.app.state.db.session_scope() as session:
        snapshot = DashboardService(session).snapshot()
        applications = list(
            session.scalars(select(Application).order_by(Application.updated_at.desc()).limit(8)).all()
        )
        runs = list(
            session.scalars(select(AutomationRun).order_by(AutomationRun.created_at.desc()).limit(6)).all()
        )
        reviewed_at = _reviewed_at(request, _OPPORTUNITY_REVIEW_COOKIE)
        new_opportunities_count = int(
            session.scalar(
                select(func.count(Opportunity.id)).where(
                    *([Opportunity.created_at > reviewed_at] if reviewed_at else [])
                )
            )
            or 0
        )
        context = {
            **_base(request, title="Command Centre", active="dashboard"),
            "snapshot": snapshot,
            "applications": applications,
            "runs": runs,
            "new_opportunities_count": new_opportunities_count,
        }
        return templates.TemplateResponse(request, "pages/dashboard.html", context)


@router.get("/opportunities", response_class=HTMLResponse)
def opportunities_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/pipeline", status_code=307)
    with request.app.state.db.session_scope() as session:
        params = _page_params(
            request,
            ("q", "source", "cycle", "programme_group", "sort", "direction", "per_page"),
        )
        page = _positive_int(request, "page", 1, 100000)
        per_page = _positive_int(request, "per_page", _PAGE_SIZE_DEFAULT, _PAGE_SIZE_MAX)
        page_data = OpportunityService(session).list_page(
            query=params["q"],
            source=params["source"],
            cycle=params["cycle"],
            programme_group=params["programme_group"],
            sort=params["sort"] or "deadline",
            direction=params["direction"] or "asc",
            page=page,
            per_page=per_page,
        )
        opportunity_ids = [record.id for record in page_data.records]
        applications_by_opportunity: dict[str, Application] = {}
        if opportunity_ids:
            applications_by_opportunity = {
                application.opportunity_id: application
                for application in session.scalars(
                    select(Application).where(Application.opportunity_id.in_(opportunity_ids))
                ).all()
            }
        reviewed_at = _reviewed_at(request, _OPPORTUNITY_REVIEW_COOKIE)
        for record in page_data.records:
            record.ui_is_new = _is_new(record.created_at, reviewed_at)
            application = applications_by_opportunity.get(record.id)
            # Keep the page context explicit; relying on a lazy one-to-one
            # relationship after the session closes made the source/application
            # boundary easy to accidentally blur in templates.
            record.ui_application = application
            record.ui_target = _target_ui(record, application)
        new_opportunities_count = int(
            session.scalar(
                select(func.count(Opportunity.id)).where(
                    *([Opportunity.created_at > reviewed_at] if reviewed_at else [])
                )
            )
            or 0
        )
        source_options = sorted(
            value
            for value in session.scalars(select(Opportunity.source).distinct()).all()
            if value
        )
        cycle_options = sorted(
            value
            for value in session.scalars(select(Opportunity.cycle).distinct()).all()
            if value
        )
        params["per_page"] = str(per_page)
        pagination = _pagination(
            "/opportunities",
            page=page_data.page,
            per_page=page_data.per_page,
            total=page_data.total_count,
            params=params,
        )
        return templates.TemplateResponse(
            request,
            "pages/opportunities.html",
            {
                **_base(request, title="Opportunities", active="opportunities"),
                "records": page_data.records,
                "total_count": page_data.total_count,
                "pagination": pagination,
                "query": params["q"],
                "source": params["source"],
                "cycle": params["cycle"],
                "programme_group": params["programme_group"],
                "sort": page_data.sort,
                "direction": page_data.direction,
                "source_options": source_options,
                "cycle_options": cycle_options,
                "reviewed_label": _review_label(reviewed_at),
                "review_url": _query_url("/opportunities/review", params),
                "new_opportunities_count": new_opportunities_count,
            },
        )


@router.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/pipeline?stage=applications", status_code=307)
    with request.app.state.db.session_scope() as session:
        params = _page_params(request, ("q", "state", "sort", "direction", "per_page"))
        page = _positive_int(request, "page", 1, 100000)
        per_page = _positive_int(request, "per_page", _PAGE_SIZE_DEFAULT, _PAGE_SIZE_MAX)
        page_data = ApplicationService(
            session,
            request.app.state.settings,
            request.app.state.crypto,
        ).list_page(
            query=params["q"],
            state=params["state"],
            sort=params["sort"] or "priority",
            direction=params["direction"] or "desc",
            page=page,
            per_page=per_page,
        )
        reviewed_at = _reviewed_at(request, _APPLICATION_REVIEW_COOKIE)
        for record in page_data.records:
            record.ui_is_new = _is_new(record.opportunity.created_at, reviewed_at)
            record.ui_target = _target_ui(record.opportunity, record)
        state_options = sorted(
            value
            for value in session.scalars(select(Application.state).distinct()).all()
            if value
        )
        params["per_page"] = str(per_page)
        pagination = _pagination(
            "/applications",
            page=page_data.page,
            per_page=page_data.per_page,
            total=page_data.total_count,
            params=params,
        )
        return templates.TemplateResponse(
            request,
            "pages/applications.html",
            {
                **_base(request, title="Applications", active="applications"),
                "records": page_data.records,
                "total_count": page_data.total_count,
                "pagination": pagination,
                "query": params["q"],
                "state": params["state"],
                "sort": page_data.sort,
                "direction": page_data.direction,
                "state_options": state_options,
                "reviewed_label": _review_label(reviewed_at),
                "review_url": _query_url("/applications/review", params),
            },
        )


def _review_redirect(
    request: Request,
    *,
    path: str,
    cookie_name: str,
) -> RedirectResponse:
    target = f"{path}?{request.url.query}" if request.url.query else path
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        cookie_name,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        # The dashboard/nav badge uses the same review marker.  Keep the
        # marker local to ARGUS while allowing those read-only pages to see it.
        path="/",
    )
    return response


@router.post("/opportunities/review", response_class=RedirectResponse)
def review_opportunities(request: Request):
    return _review_redirect(
        request,
        path="/opportunities",
        cookie_name=_OPPORTUNITY_REVIEW_COOKIE,
    )


@router.post("/applications/review", response_class=RedirectResponse)
def review_applications(request: Request):
    return _review_redirect(
        request,
        path="/applications",
        cookie_name=_APPLICATION_REVIEW_COOKIE,
    )


@router.get("/applications/{application_id}", response_class=HTMLResponse)
def application_detail_page(application_id: str, request: Request):
    if pages_v2.ui_v2_enabled(request):
        return pages_v2.render_application_detail_by_id(request, application_id)
    with request.app.state.db.session_scope() as session:
        application = session.scalar(
            select(Application)
            .options(
                selectinload(Application.opportunity),
                selectinload(Application.runs),
                selectinload(Application.selected_cv),
                selectinload(Application.selected_cover_letter),
            )
            .where(Application.id == application_id)
        )
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        runs = list(
            session.scalars(
                select(AutomationRun)
                .options(selectinload(AutomationRun.questions))
                .where(AutomationRun.application_id == application.id)
                .order_by(AutomationRun.created_at.desc())
            ).all()
        )
        audits = list(
            session.scalars(
                select(AuditEvent)
                .where(_application_audit_filter(application.id, runs))
                .order_by(AuditEvent.id.desc())
            ).all()
        )
        context = {
            **_base(request, title="Application detail", active="applications"),
            **_application_detail_context(application, runs, audits, request),
        }
        return templates.TemplateResponse(request, "pages/application_detail.html", context)


@router.get("/needs-you", response_class=HTMLResponse)
def needs_you_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return pages_v2.render_needs_you(request)
    with request.app.state.db.session_scope() as session:
        records = [
            {
                "application": application,
                "opportunity": application.opportunity,
                "state": application.state,
                "target": _target_ui(application.opportunity, application),
            }
            for application in session.scalars(
                select(Application)
                .options(selectinload(Application.opportunity))
                .where(
                    Application.state.in_(
                        ["NEEDS_USER", "NEEDS_OA", "FAILED_RETRYABLE", "BLOCKED", "READY_TO_SUBMIT", "SUBMISSION_UNKNOWN"]
                    )
                )
                .order_by(Application.priority.desc())
                .limit(100)
            ).all()
        ]
        return templates.TemplateResponse(
            request,
            "pages/needs_you.html",
            {**_base(request, title="Needs You", active="needs-you"), "records": records},
        )


@router.get("/needs-you/{application_id}", response_class=RedirectResponse)
def needs_you_application(application_id: str, request: Request):
    """Resolve a notification deep link to its exact local application."""

    # The deep link is followed from a notification, so the target must be
    # proven to exist on BOTH UI paths -- the v2 branch used to redirect
    # unconditionally, turning a link to a deleted application into a silent
    # bounce instead of a 404.  303 (See Other) is also the correct code for
    # a GET that resolves to a different resource; 307 would preserve the
    # method and is meant for the v2 shell's own route aliases.
    with request.app.state.db.session_scope() as session:
        if session.get(Application, application_id) is None:
            raise HTTPException(status_code=404, detail="Application not found")
    return RedirectResponse(
        f"/applications/{quote(application_id, safe='')}", status_code=303
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/library?tab=profile", status_code=307)
    with request.app.state.db.session_scope() as session:
        profile = ProfileService(session, request.app.state.crypto).public_dict()
        return templates.TemplateResponse(
            request,
            "pages/profile.html",
            {**_base(request, title="Candidate Profile", active="profile"), "profile": profile},
        )


@router.get("/answers", response_class=HTMLResponse)
def answers_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/library?tab=answers", status_code=307)
    with request.app.state.db.session_scope() as session:
        records = AnswerService(session, request.app.state.crypto).list_public()
        return templates.TemplateResponse(
            request,
            "pages/answers.html",
            {**_base(request, title="Answer Bank", active="answers"), "records": records},
        )


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/library?tab=documents", status_code=307)
    with request.app.state.db.session_scope() as session:
        records = list(session.scalars(select(Document).order_by(Document.created_at.desc())).all())
        for record in records:
            record.ui_tags = json.loads(record.tags_json or "[]")
        return templates.TemplateResponse(
            request,
            "pages/documents.html",
            {**_base(request, title="Documents", active="documents"), "records": records},
        )


@router.get("/mail", response_class=HTMLResponse)
def mail_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/activity?tab=mail", status_code=307)
    with request.app.state.db.session_scope() as session:
        records = list(
            session.scalars(select(EmailMessage).order_by(EmailMessage.received_at.desc())).all()
        )
        return templates.TemplateResponse(
            request,
            "pages/mail.html",
            {**_base(request, title="Mail & Assessments", active="mail"), "records": records},
        )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/activity?tab=audit", status_code=307)
    with request.app.state.db.session_scope() as session:
        records = list(
            session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)).all()
        )
        return templates.TemplateResponse(
            request,
            "pages/audit.html",
            {**_base(request, title="Audit Trail", active="audit"), "records": records},
        )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if pages_v2.ui_v2_enabled(request):
        return RedirectResponse("/control", status_code=307)
    with request.app.state.db.session_scope() as session:
        rules = [
            {
                "id": record.id,
                "employer_pattern": record.employer_pattern,
                "cycle": record.cycle,
                "max_applications": record.max_applications,
                "exclusive_groups": json.loads(record.exclusive_groups_json or "[]"),
                "notes": record.notes,
            }
            for record in session.scalars(
                select(ConflictRule).order_by(ConflictRule.employer_pattern, ConflictRule.cycle)
            ).all()
        ]
        return templates.TemplateResponse(
            request,
            "pages/settings.html",
            {
                **_base(request, title="Safety Settings", active="settings"),
                "settings": request.app.state.settings,
                "conflict_rules": rules,
            },
        )
