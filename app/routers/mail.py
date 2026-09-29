from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.models import EmailMessage
from app.routers.deps import SessionDep
from app.services.email import MailService

router = APIRouter(prefix="/api/mail", tags=["mail"])


class MatchOverridePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str | None = Field(default=None, max_length=80)


def _serialize(item: EmailMessage) -> dict[str, object]:
    return {
        "id": item.id,
        "message_id": item.message_id,
        "sender": item.sender,
        "subject": item.subject,
        "received_at": item.received_at.isoformat(),
        "classification": item.classification,
        "application_id": item.application_id,
        "action_deadline": item.action_deadline.isoformat() if item.action_deadline else None,
    }


@router.get("")
def list_mail(session: SessionDep) -> list[dict[str, object]]:
    records = list(
        session.scalars(select(EmailMessage).order_by(EmailMessage.received_at.desc())).all()
    )
    return [_serialize(item) for item in records]


@router.post("/ingest", status_code=201)
def ingest_mail(
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "Email exceeds 5 MiB")
    try:
        return _serialize(MailService(session).ingest(content))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{email_id}/candidates")
def match_candidates(email_id: str, session: SessionDep) -> dict[str, object]:
    try:
        candidates = MailService(session).match_candidates(email_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"email_id": email_id, "candidates": candidates}


@router.post("/{email_id}/match")
def override_match(
    email_id: str, payload: MatchOverridePayload, session: SessionDep
) -> dict[str, object]:
    try:
        record = MailService(session).override_match(email_id, payload.application_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _serialize(record)
