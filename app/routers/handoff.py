"""Queue-only HTTP surface for persistent application browser sessions."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.automation.types import RunMode, SessionSnapshot
from app.domain.states import ApplicationState, user_status_exclusion_reason
from app.models import Application, Document
from app.services.documents import DocumentService
from app.automation.host_policy import origin_for_url
from app.services.submission_authority import (
    SubmissionAuthorityError,
    SubmissionAuthorityService,
    SubmissionReviewBindingService,
    authority_manifest_projection,
)
from app.services.navigator import (
    canonical_target_contract_url,
    DuplicateSessionError,
    NavigatorError,
    PersistedTargetResolutionError,
    SessionNotFoundError,
    _load_verified_target_from_opportunity,
)

router = APIRouter(prefix="/api/handoff", tags=["handoff"])


class StartSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str | None = None
    mode: RunMode | None = None


class SessionApplicationBody(BaseModel):
    """Bind every state-changing session command to its exact application."""

    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1)


ConfirmSessionBody = SessionApplicationBody


_SAFE_REVIEW_START_STATES = frozenset(
    {
        ApplicationState.ELIGIBILITY_CHECKED.value,
        ApplicationState.QUEUED.value,
        ApplicationState.PACKAGE_PREPARED.value,
        ApplicationState.FILLING.value,
        ApplicationState.NEEDS_USER.value,
        ApplicationState.FAILED_RETRYABLE.value,
        ApplicationState.READY_TO_SUBMIT.value,
    }
)


def _navigator(request: Request):
    navigator = getattr(request.app.state, "navigator", None)
    if navigator is None:
        # Compatibility with applications created during the transition.
        navigator = getattr(request.app.state, "handoff_manager", None)
    if navigator is None:
        raise HTTPException(503, "Application navigator is not available")
    return navigator


def _application_state(request: Request, application_id: str) -> str:
    """Read the application state without allowing a start-time mutation."""

    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(503, "Application database is not available")
    with database.SessionLocal() as session:
        application = session.get(Application, application_id)
        if application is None:
            raise HTTPException(404, f"Application not found: {application_id}")
        return str(application.state)


def _application_user_status(request: Request, application_id: str) -> str:
    """Read candidate-owned status before a Navigator worker is created."""

    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(503, "Application database is not available")
    with database.SessionLocal() as session:
        application = session.get(Application, application_id)
        if application is None:
            raise HTTPException(404, f"Application not found: {application_id}")
        return str(application.opportunity.user_status)


def _assert_start_allowed(request: Request, application_id: str, mode: RunMode) -> None:
    """Fail closed before any navigator worker can be created."""

    if mode is not RunMode.REVIEW:
        raise HTTPException(
            409,
            "Navigator handoff starts are review-only; submission mode is not supported",
        )
    status_reason = user_status_exclusion_reason(
        _application_user_status(request, application_id)
    )
    if status_reason is not None:
        raise HTTPException(409, status_reason)
    state = _application_state(request, application_id)
    if state not in _SAFE_REVIEW_START_STATES:
        raise HTTPException(
            409,
            f"Application state {state!r} cannot start or resume a Navigator review",
        )


def _payload(
    snapshot: SessionSnapshot,
    *,
    manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return only serialisable evidence; never expose Playwright handles."""

    return {
        "session_id": snapshot.session_id,
        "application_id": snapshot.application_id,
        "mode": snapshot.mode,
        "state": snapshot.state.value,
        "status": snapshot.state.value,
        "reason": snapshot.reason,
        "outcome": snapshot.outcome,
        "summary": dict(snapshot.summary),
        "manifest": dict(snapshot.manifest) if manifest is None else dict(manifest),
        "created_at": snapshot.created_at.isoformat(),
        "updated_at": snapshot.updated_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "owner_thread_id": snapshot.owner_thread_id,
        "worker_alive": snapshot.worker_alive,
        "cleanup_complete": snapshot.cleanup_complete,
        "headed": snapshot.headed,
        "visibility": snapshot.visibility,
        "human_boundary": dict(snapshot.human_boundary),
        "resumable": snapshot.resumable,
        "resume_until": (
            snapshot.resume_until.isoformat()
            if snapshot.resume_until is not None
            else None
        ),
    }


def _selected_document_manifest(
    request: Request,
    db_session: Any,
    application: Application,
) -> list[dict[str, object]]:
    """Return every selected document or fail closed before authority issue."""

    verifier = DocumentService(db_session, request.app.state.settings.documents_dir)
    documents: list[dict[str, object]] = []
    for kind, document_id in (
        ("document.cv", application.selected_cv_id),
        ("document.cover_letter", application.selected_cover_letter_id),
    ):
        if not document_id:
            continue
        document = db_session.get(Document, document_id)
        if document is None:
            raise HTTPException(409, f"Selected {kind} document no longer exists")
        if not document.approved:
            raise HTTPException(409, f"Selected {kind} document is no longer approved")
        if not verifier.verify(document):
            raise HTTPException(
                409,
                f"Selected {kind} document bytes or stored hash changed after review",
            )
        documents.append(
            {
                "id": str(document.id),
                "kind": kind,
                "sha256": str(document.sha256),
                "approved": True,
            }
        )
    return sorted(documents, key=lambda item: (str(item["kind"]), str(item["id"])))


def _first_evidence_text(evidence: Any, *keys: str) -> str:
    if not isinstance(evidence, dict):
        return ""
    for key in keys:
        value = evidence.get(key)
        if value is not None and not isinstance(value, (dict, list, tuple, set)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _authority_manifest(
    request: Request,
    snapshot: SessionSnapshot,
) -> dict[str, object]:
    """Rebuild the exact display/authority manifest from current server truth."""

    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(503, "Application database is not available")
    with database.SessionLocal() as db_session:
        application = db_session.get(Application, snapshot.application_id)
        if application is None:
            raise HTTPException(404, "Application not found")
        status_reason = user_status_exclusion_reason(
            application.opportunity.user_status
        )
        if status_reason is not None:
            raise HTTPException(409, status_reason)
        if str(application.state) not in _SAFE_REVIEW_START_STATES:
            raise HTTPException(
                409,
                f"Application state {application.state!r} cannot issue submission authority",
            )
        try:
            eligibility = json.loads(application.eligibility_json or "{}")
            conflict = json.loads(application.conflict_json or "{}")
        except (TypeError, ValueError) as exc:
            raise HTTPException(409, "Eligibility or conflict evidence is malformed") from exc
        if isinstance(eligibility, dict) and eligibility.get("eligible") is False:
            raise HTTPException(409, "Application eligibility is currently blocked")
        if isinstance(conflict, dict) and conflict.get("blocked") is True:
            raise HTTPException(409, "Application has a current employer conflict")
        if application.risk_is_assessed and application.effective_risk_level != 0:
            raise HTTPException(409, "Only a current risk-zero review can issue submission authority")
        try:
            resolution = _load_verified_target_from_opportunity(application.opportunity)
        except PersistedTargetResolutionError as exc:
            raise HTTPException(409, str(exc)) from exc

        manifest = dict(snapshot.manifest)
        if not manifest or str(manifest.get("application_id")) != str(application.id):
            raise HTTPException(409, "Final manifest is missing or bound to another application")
        if str(manifest.get("employer") or "") != str(application.opportunity.employer):
            raise HTTPException(409, "Final manifest employer no longer matches persisted target")
        if str(manifest.get("role") or "") != str(application.opportunity.role_title):
            raise HTTPException(409, "Final manifest role no longer matches persisted target")
        if str(manifest.get("provider") or "").casefold() != str(resolution.provider).casefold():
            raise HTTPException(409, "Final manifest provider no longer matches persisted target")
        try:
            application_contract = canonical_target_contract_url(
                str(manifest.get("application_url") or "")
            )
            persisted_contract = canonical_target_contract_url(resolution.final_url)
        except (TypeError, ValueError) as exc:
            raise HTTPException(409, "Final manifest application URL is malformed") from exc
        if application_contract != persisted_contract:
            raise HTTPException(
                409,
                "Final manifest application path no longer matches persisted target",
            )
        manifest_origin = origin_for_url(
            str(manifest.get("destination") or manifest.get("form_action") or "")
        )
        persisted_origin = origin_for_url(resolution.final_url)
        if not manifest_origin or manifest_origin != persisted_origin:
            raise HTTPException(409, "Final manifest destination no longer matches persisted target")

        evidence = dict(resolution.evidence)
        requisition = _first_evidence_text(
            evidence,
            "requisition",
            "requisition_id",
            "job_id",
            "posting_id",
            "target_path",
            "application_path",
        )
        if not requisition:
            raise HTTPException(409, "Verified requisition identity is missing")
        if str(manifest.get("requisition") or "").strip() != requisition:
            raise HTTPException(
                409,
                "Final manifest requisition no longer matches persisted target",
            )
        form_identity = _first_evidence_text(
            evidence,
            "form_identity",
            "bound_form_identity",
            "application_form",
            "form_id",
        )
        if not form_identity:
            form_identity = str(manifest.get("form_identity") or "").strip()
        if not form_identity:
            raise HTTPException(409, "Verified form identity is missing")
        if str(manifest.get("form_identity") or "").strip() != form_identity:
            raise HTTPException(
                409,
                "Final manifest form identity no longer matches persisted target",
            )
        if str(manifest.get("method") or "").strip().upper() != "POST":
            raise HTTPException(409, "Final manifest method is not the verified POST action")
        form_action_origin = origin_for_url(str(manifest.get("form_action") or ""))
        frame_origin = origin_for_url(str(manifest.get("frame_url") or ""))
        expected_final_origin = origin_for_url(
            str(manifest.get("expected_final_url") or "")
        )
        if (
            form_action_origin != persisted_origin
            or frame_origin != persisted_origin
            or expected_final_origin != persisted_origin
        ):
            raise HTTPException(
                409,
                "Final form action, frame, or receipt target escaped the persisted origin",
            )
        try:
            if canonical_target_contract_url(
                str(manifest.get("destination") or "")
            ) != canonical_target_contract_url(str(manifest.get("form_action") or "")):
                raise HTTPException(
                    409,
                    "Final destination is not the exact data-bearing form action",
                )
        except (TypeError, ValueError) as exc:
            raise HTTPException(409, "Final form action or destination is malformed") from exc
        manifest["application_id"] = str(application.id)
        manifest["employer"] = str(application.opportunity.employer)
        manifest["role"] = str(application.opportunity.role_title)
        manifest["provider"] = str(resolution.provider)
        manifest["application_url"] = resolution.final_url
        manifest["requisition"] = requisition
        manifest["form_identity"] = form_identity
        manifest["documents"] = _selected_document_manifest(
            request,
            db_session,
            application,
        )
        # Calling the strict projection here validates that the exact manifest
        # shown to the user is also sufficient to issue and later consume.
        authority_manifest_projection(manifest)
        return manifest


def _get(navigator: Any, session_id: str) -> SessionSnapshot:
    try:
        return navigator.get(session_id)
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


def _record_review_binding(
    request: Request,
    snapshot: SessionSnapshot,
    manifest: dict[str, object],
) -> None:
    """Durably bind exactly what the final-manifest response displays."""

    destination = str(manifest.get("destination") or "")
    origin = origin_for_url(destination)
    if not origin:
        raise HTTPException(409, "Final manifest has no valid destination origin")
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(503, "Application database is not available")
    try:
        with database.session_scope() as db_session:
            SubmissionReviewBindingService(db_session).record(
                application_id=snapshot.application_id,
                session_id=snapshot.session_id,
                manifest=manifest,
                destination_origin=origin,
                expires_at=snapshot.expires_at,
            )
    except SubmissionAuthorityError as exc:
        raise HTTPException(409, str(exc)) from exc


def _validate_review_binding(
    request: Request,
    snapshot: SessionSnapshot,
    manifest: dict[str, object],
    *,
    db_session: Any | None = None,
) -> None:
    destination = str(manifest.get("destination") or "")
    origin = origin_for_url(destination)
    if not origin:
        raise HTTPException(409, "Final manifest has no valid destination origin")
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(503, "Application database is not available")
    try:
        if db_session is not None:
            SubmissionReviewBindingService(db_session).validate(
                application_id=snapshot.application_id,
                session_id=snapshot.session_id,
                manifest=manifest,
                destination_origin=origin,
            )
        else:
            with database.SessionLocal() as session:
                SubmissionReviewBindingService(session).validate(
                    application_id=snapshot.application_id,
                    session_id=snapshot.session_id,
                    manifest=manifest,
                    destination_origin=origin,
                )
    except SubmissionAuthorityError as exc:
        raise HTTPException(409, str(exc)) from exc


def _bound_session(
    navigator: Any, session_id: str, application_id: str
) -> SessionSnapshot:
    """Return a session only when the caller supplied its exact app id."""

    snapshot = _get(navigator, session_id)
    if str(application_id) != str(snapshot.application_id):
        raise HTTPException(
            409,
            "Exact application_id does not match the Navigator session",
        )
    return snapshot


def _start(request: Request, application_id: str, mode: RunMode) -> SessionSnapshot:
    _assert_start_allowed(request, application_id, mode)
    navigator = _navigator(request)
    try:
        return navigator.start(application_id, mode)
    except DuplicateSessionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        # An unresolved, non-HTML, mismatched, or otherwise unsafe target is
        # a conflict with starting a local Navigator session, not malformed
        # user input.  Preserve this distinction for UI/tool callers.
        raise HTTPException(409, str(exc)) from exc


@router.post("/applications/{application_id}/start", status_code=202)
def start_application(application_id: str, request: Request, mode: RunMode = RunMode.REVIEW):
    return _payload(_start(request, application_id, mode))


@router.post("/start/{application_id}", status_code=202)
def start_application_alias(application_id: str, request: Request, mode: RunMode = RunMode.REVIEW):
    """Short local-tool alias for clients that do not namespace applications."""

    return _payload(_start(request, application_id, mode))


@router.post("/sessions", status_code=202)
def start_session(
    request: Request,
    application_id: str | None = None,
    mode: RunMode | None = None,
    body: StartSessionBody | None = Body(default=None),
):
    """Tool-friendly equivalent of the application-scoped start route."""

    application_id = application_id or (body.application_id if body else None)
    mode = mode or (body.mode if body else None) or RunMode.REVIEW
    if not application_id:
        raise HTTPException(400, "application_id is required")
    return _payload(_start(request, application_id, mode))


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict[str, object]]:
    navigator = _navigator(request)
    return [_payload(snapshot) for snapshot in navigator.all_sessions()]


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, object]:
    return _payload(_get(_navigator(request), session_id))


@router.get("/sessions/{session_id}/status")
def get_session_status(session_id: str, request: Request) -> dict[str, object]:
    return _payload(_get(_navigator(request), session_id))


@router.post("/sessions/{session_id}/continue")
def continue_session(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    navigator = _navigator(request)
    _bound_session(navigator, session_id, body.application_id)
    try:
        return _payload(navigator.continue_after_human(session_id))
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/sessions/{session_id}/continue-after-human")
def continue_after_human(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    return continue_session(session_id, request, body)


@router.post("/sessions/{session_id}/resume")
def resume_expired_session(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    """Renew an expired TTL while preserving the service-owned page/context."""

    navigator = _navigator(request)
    _bound_session(navigator, session_id, body.application_id)
    try:
        return _payload(navigator.resume(session_id))
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except NavigatorError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sessions/{session_id}/final-manifest")
def final_manifest(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    navigator = _navigator(request)
    _bound_session(navigator, session_id, body.application_id)
    try:
        snapshot = navigator.request_final_manifest(session_id)
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    if snapshot.state.value != "FINAL_REVIEW":
        return _payload(snapshot)
    manifest = _authority_manifest(request, snapshot)
    _record_review_binding(request, snapshot, manifest)
    seal_manifest = getattr(navigator, "seal_manifest", None)
    if callable(seal_manifest):
        try:
            seal_manifest(session_id, snapshot.manifest)
        except NavigatorError as exc:
            raise HTTPException(409, str(exc)) from exc
    return _payload(snapshot, manifest=manifest)


@router.post("/sessions/{session_id}/manifest")
def request_manifest(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    return final_manifest(session_id, request, body)


@router.post("/sessions/{session_id}/confirm")
def confirm_session(
    session_id: str,
    request: Request,
    body: ConfirmSessionBody = Body(...),
) -> dict[str, object]:
    navigator = _navigator(request)
    snapshot = _bound_session(navigator, session_id, body.application_id)
    if snapshot.state.value != "FINAL_REVIEW":
        return _payload(navigator.confirm(session_id))
    # This is the same enriched manifest returned by /final-manifest.  It is
    # reconstructed from server truth at action time so selected-document or
    # persisted-target mutation cannot be hidden between review and confirm.
    manifest = _authority_manifest(request, snapshot)
    _validate_review_binding(request, snapshot, manifest)
    destination = str(manifest.get("destination") or manifest.get("final_url") or "")
    origin = origin_for_url(destination)
    if not origin:
        raise HTTPException(409, "Final manifest has no valid destination origin")
    try:
        confirmed = navigator.confirm(session_id)
        wait_for_terminal = getattr(navigator, "wait_for_terminal", None)
        if callable(wait_for_terminal):
            confirmed = wait_for_terminal(session_id, timeout=5)
        cleanup_ok = bool(getattr(confirmed, "cleanup_complete", True))
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    confirmed_manifest = _authority_manifest(request, confirmed)
    _validate_review_binding(request, confirmed, confirmed_manifest)
    if (
        confirmed.state.value != "CONFIRMED"
        or str(confirmed.application_id) != str(body.application_id)
        or str(confirmed.session_id) != str(session_id)
        or not cleanup_ok
        or authority_manifest_projection(confirmed_manifest)
        != authority_manifest_projection(manifest)
    ):
        raise HTTPException(409, "Navigator confirmation was not exact, current, and fully cleaned up")
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(503, "Application database is not available")
    try:
        with database.session_scope() as db_session:
            application = db_session.get(Application, body.application_id)
            if application is None:
                raise HTTPException(404, "Application not found")
            # Re-read once more inside the issuing transaction.  The helper
            # owns its own read session, so compare projections here against
            # the final action-time reconstruction before inserting a row.
            issue_manifest = _authority_manifest(request, confirmed)
            _validate_review_binding(
                request,
                confirmed,
                issue_manifest,
                db_session=db_session,
            )
            if authority_manifest_projection(issue_manifest) != authority_manifest_projection(manifest):
                raise HTTPException(409, "Submission inputs changed during confirmation")
            authority = SubmissionAuthorityService(db_session).issue(
                application_id=body.application_id,
                session_id=session_id,
                manifest=issue_manifest,
                destination_origin=origin,
                expires_in_seconds=300,
            )
            authority_payload = {
                "authority": {
                    "authority_id": authority.id,
                    "expires_at": authority.expires_at.isoformat(),
                    "application_id": authority.application_id,
                    "session_id": authority.session_id,
                    "manifest": {
                        key: issue_manifest.get(key)
                        for key in (
                            "employer", "role", "requisition", "provider", "destination",
                            "target_fingerprint", "control_fingerprint", "form_identity",
                            "documents", "answers",
                        )
                        if issue_manifest.get(key) is not None
                    },
                }
            }
    except SubmissionAuthorityError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _payload(confirmed) | authority_payload


@router.post("/sessions/{session_id}/cancel")
def cancel_session(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    navigator = _navigator(request)
    _bound_session(navigator, session_id, body.application_id)
    try:
        return _payload(navigator.cancel(session_id, reason="user cancelled from UI"))
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/sessions/{session_id}/close")
def close_session(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    navigator = _navigator(request)
    _bound_session(navigator, session_id, body.application_id)
    try:
        return _payload(navigator.close(session_id, reason="closed without submission"))
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/sessions/{session_id}/complete")
def deprecated_complete_session(
    session_id: str,
    request: Request,
    body: SessionApplicationBody = Body(...),
) -> dict[str, object]:
    """Compatibility endpoint; ``Complete`` is explicitly *not* submission."""

    navigator = _navigator(request)
    _bound_session(navigator, session_id, body.application_id)
    try:
        snapshot = navigator.close(session_id, reason="deprecated complete means close only")
    except (KeyError, SessionNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return _payload(snapshot)
