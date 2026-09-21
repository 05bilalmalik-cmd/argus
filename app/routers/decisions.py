from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.domain.questions import CanonicalKey
from app.routers.deps import SessionDep
from app.services import decision_store as answer_store
from app.services.decisions import (
    LEGAL_SENSITIVE_KEYS,
    DecisionRequest,
    DecisionResponse,
    build_decision_requests,
    context_revision_for_request,
    stable_decision_id,
)

router = APIRouter(prefix="/api", tags=["api"])

#: Router-local alias kept so existing importers keep working. The set
#: itself is owned by app.services.decisions (single source).
LEGAL_SENSITIVE_KEYS = LEGAL_SENSITIVE_KEYS

ACKNOWLEDGEMENT_NOTE = (
    "Answering records a human acknowledgement only. It is not field "
    "filling, CAPTCHA completion, or application submission, and it never "
    "arms automation. The listed action labels (for example 'Upload CV' "
    "or 'Completed') are manual next steps, not proof that they happened."
)


def _get_decision_token() -> str | None:
    """Read the decision auth token from environment. Returns None if unset/empty."""
    token = os.environ.get("ARGUS_DECISION_TOKEN", "").strip()
    return token if token else None


def _verify_token(provided: str | None, expected: str) -> bool:
    """Constant-time token comparison using hmac.compare_digest."""
    if provided is None:
        return False
    try:
        return hmac.compare_digest(provided.encode("ascii"), expected.encode("ascii"))
    except (UnicodeEncodeError, TypeError, AttributeError):
        return False


def _require_token(provided: str | None) -> str:
    """Shared auth gate: 404 when the endpoint is inactive, 401 when wrong."""
    expected_token = _get_decision_token()
    if expected_token is None:
        raise HTTPException(status_code=404, detail="Not found")
    if not _verify_token(provided, expected_token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return expected_token


def _stable_decision_id(
    application_id: str,
    canonical_key: CanonicalKey,
    *,
    context_revision: str,
) -> str:
    """Context-bound deterministic decision ID (delegates to the service)."""
    return stable_decision_id(
        application_id, canonical_key, context_revision=context_revision
    )


def _build_stable_requests(session: Session) -> list[DecisionRequest]:
    """Build decision requests with stable, context-bound deterministic IDs."""
    requests = build_decision_requests(session)
    stable_requests: list[DecisionRequest] = []
    for req in requests:
        revision = context_revision_for_request(req)
        stable_requests.append(
            DecisionRequest(
                id=_stable_decision_id(
                    req.application_id, req.canonical_key, context_revision=revision
                ),
                application_id=req.application_id,
                employer=req.employer,
                role=req.role,
                question_text=req.question_text,
                canonical_key=req.canonical_key,
                sensitivity=req.sensitivity,
                permitted_options=req.permitted_options,
                confidence=req.confidence,
                prompt=req.prompt,
            )
        )
    return stable_requests


def _load_answer_records(settings) -> dict[str, dict[str, object]]:
    """Load stored answers, failing closed (503) when the store is corrupt."""
    try:
        return answer_store.read_records(settings)
    except answer_store.DecisionStoreCorruptError as exc:
        raise HTTPException(
            status_code=503,
            detail="Decision store unavailable; no answer recorded or returned",
        ) from exc


class AnswerPayload(BaseModel):
    """Inbound human decision answer."""

    model_config = ConfigDict(extra="forbid")

    chosen_option: str = Field(min_length=1, max_length=500)
    decided_by: str = Field(min_length=1, max_length=200)


@router.get("/decisions")
def list_decisions(
    request: Request,
    session: SessionDep,
    x_decision_token: Annotated[str | None, Header()] = None,
) -> list[dict[str, object]]:
    """List currently pending decision requests. Requires valid token.

    Answered questions stay listed while their application still needs a
    human: acknowledgement never suppresses unresolved work.
    """
    _require_token(x_decision_token)

    requests = _build_stable_requests(session)
    return [req.to_dict() for req in requests]


@router.get("/decisions/{decision_id}")
def read_decision(
    decision_id: str,
    request: Request,
    session: SessionDep,
    x_decision_token: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Authenticated readback: current request plus recorded response.

    ``status`` is ``answered`` (this context answered), ``unresolved``
    (this context awaiting a human), or ``stale_or_resolved`` (an answer
    exists but no current question uses this ID). ``manual_action_needed``
    is always true: an answer never constitutes field filling, CAPTCHA
    completion, or submission.
    """
    _require_token(x_decision_token)

    requests = _build_stable_requests(session)
    request_map = {req.id: req for req in requests}
    records = _load_answer_records(request.app.state.settings)

    current = request_map.get(decision_id)
    if current is not None:
        record = records.get(decision_id)
        if record is not None and str(record.get("application_id")) != current.application_id:
            raise HTTPException(status_code=404, detail="Decision not found")
        return {
            "decision_id": decision_id,
            "status": "answered" if record is not None else "unresolved",
            "manual_action_needed": True,
            "request": current.to_dict(),
            "answer": record,
            "note": ACKNOWLEDGEMENT_NOTE,
        }

    record = records.get(decision_id)
    if record is not None:
        return {
            "decision_id": decision_id,
            "status": "stale_or_resolved",
            "manual_action_needed": True,
            "request": None,
            "answer": record,
            "note": ACKNOWLEDGEMENT_NOTE,
        }
    raise HTTPException(status_code=404, detail="Decision not found")


@router.post("/decisions/{decision_id}/answer")
def answer_decision(
    decision_id: str,
    payload: AnswerPayload,
    request: Request,
    session: SessionDep,
    x_decision_token: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Record a human's answer to a decision request. Requires valid token."""
    _require_token(x_decision_token)

    requests = _build_stable_requests(session)
    request_map = {req.id: req for req in requests}

    if decision_id not in request_map:
        records = _load_answer_records(request.app.state.settings)
        if decision_id in records:
            raise HTTPException(
                status_code=404, detail="Decision not found or stale"
            )
        raise HTTPException(status_code=404, detail="Decision not found")

    decision_request = request_map[decision_id]

    # Sensitive tier checks FIRST - must have explicit choice, no default
    if decision_request.canonical_key in LEGAL_SENSITIVE_KEYS:
        if not payload.chosen_option or payload.chosen_option.strip().casefold() == "default":
            raise HTTPException(
                status_code=400,
                detail="Sensitive tier requires explicit choice; no default permitted",
            )

    if payload.chosen_option not in decision_request.permitted_options:
        raise HTTPException(
            status_code=400,
            detail=f"Chosen option not in permitted options: {decision_request.permitted_options}",
        )

    # Fail closed before validating further: a corrupt store must refuse
    # the write rather than risk a duplicate or a lost acknowledgement.
    _load_answer_records(request.app.state.settings)

    decided_at = datetime.now(timezone.utc)
    response = DecisionResponse(
        decision_id=decision_id,
        chosen_option=payload.chosen_option,
        decided_by=payload.decided_by,
        decided_at=decided_at,
    )

    decision_request.validate_response(response)

    revision = context_revision_for_request(decision_request)
    record = {
        "decision_id": response.decision_id,
        "chosen_option": response.chosen_option,
        "decided_by": response.decided_by,
        "decided_at": response.decided_at.isoformat(),
        "application_id": decision_request.application_id,
        "canonical_key": decision_request.canonical_key.value,
        "sensitivity": decision_request.sensitivity.value,
        "context_revision": revision,
        "question_text": decision_request.question_text,
        "permitted_options": list(decision_request.permitted_options),
        "employer": decision_request.employer,
        "role": decision_request.role,
    }

    try:
        recorded = answer_store.try_record(request.app.state.settings, record)
    except answer_store.DecisionStoreCorruptError as exc:
        raise HTTPException(
            status_code=503,
            detail="Decision store unavailable; no answer recorded or returned",
        ) from exc
    if not recorded:
        raise HTTPException(status_code=409, detail="Decision already answered")

    return {
        "decision_id": response.decision_id,
        "chosen_option": response.chosen_option,
        "decided_by": response.decided_by,
        "decided_at": response.decided_at.isoformat(),
        "context_revision": revision,
    }
