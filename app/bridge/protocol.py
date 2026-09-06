"""Closed preparation wire schema; no application/runtime dependencies."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_REQUEST_BYTES = 16 * 1024
MAX_REPLY_BYTES = 64 * 1024
PROTOCOL = 'argus.preparation.v1'
CanonicalUUID = Annotated[str, StringConstraints(
    pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    min_length=36, max_length=36,
)]
IdempotencyKey = Annotated[str, StringConstraints(
    pattern=r'^[A-Za-z0-9_-]{8,64}$', min_length=8, max_length=64,
)]


class ClosedModel(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', hide_input_in_errors=True)


class EmptyBody(ClosedModel):
    pass


class PreparationBody(ClosedModel):
    idempotency_key: IdempotencyKey


class RunBody(ClosedModel):
    run_id: CanonicalUUID


class HandoffsBody(ClosedModel):
    after_cursor: CanonicalUUID | None
    limit: Annotated[int, Field(ge=1, le=100)]


class PauseBody(ClosedModel):
    reason_code: Literal['OPERATOR_REQUESTED', 'INTEGRITY', 'TRANSPORT_ERROR']


BODY_MODELS = {
    'get_preparation_readiness': EmptyBody,
    'request_approved_preparation': PreparationBody,
    'get_preparation_run': RunBody,
    'list_preparation_handoffs': HandoffsBody,
    'pause_preparation': PauseBody,
}


class Request(ClosedModel):
    operation: Literal[
        'get_preparation_readiness', 'request_approved_preparation',
        'get_preparation_run', 'list_preparation_handoffs', 'pause_preparation',
    ]
    body: dict


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('INVALID_REQUEST')
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError('INVALID_REQUEST')


def load_json(raw: bytes, *, maximum: int) -> object:
    """Bounded strict UTF-8 JSON; duplicate keys and nonfinite numbers fail closed."""
    try:
        if type(raw) is not bytes or len(raw) > maximum:
            raise ValueError('INVALID_REQUEST')
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise ValueError('INVALID_REQUEST') from None


Status = Literal[
    'READY', 'DISABLED', 'PAUSED', 'BUSY', 'RUNNING', 'PREFILLED_HANDOFF',
    'HUMAN_REQUIRED', 'BLOCKED', 'FAILED', 'INTERRUPTED', 'REFUSED',
]
Reason = Literal[
    'NONE', 'DISABLED', 'UNAUTHENTICATED', 'INVALID_REQUEST', 'NO_APPROVAL',
    'EXPIRED', 'REVOKED', 'PAUSED', 'BUSY', 'DRIFT', 'NOT_READY', 'HANDOFF',
    'INTEGRITY', 'INTERRUPTED', 'EXECUTION_FAILED', 'NOT_FOUND', 'TRANSPORT_ERROR',
]


class Handoff(ClosedModel):
    run_id: CanonicalUUID
    status: Status
    reason: Reason


class Reply(ClosedModel):
    protocol: Literal['argus.preparation.v1']
    status: Status
    reason: Reason
    run_id: CanonicalUUID | None
    pending: Annotated[int, Field(ge=0, le=100000)]
    handoffs: Annotated[list[Handoff], Field(max_length=100)]
    next_cursor: CanonicalUUID | None


def validate_reply(raw: dict) -> dict:
    try:
        return Reply.model_validate(raw).model_dump()
    except (ValueError, TypeError, RecursionError):
        raise ValueError('INVALID_REQUEST') from None


def refusal(reason: str) -> dict:
    from typing import get_args

    return {
        'protocol': PROTOCOL, 'status': 'REFUSED',
        'reason': reason if type(reason) is str and reason in get_args(Reason)
        else 'INVALID_REQUEST',
        'run_id': None, 'pending': 0, 'handoffs': [], 'next_cursor': None,
    }


def parse_request(raw_bytes: bytes) -> Request:
    try:
        request = Request.model_validate(load_json(raw_bytes, maximum=MAX_REQUEST_BYTES))
        request.body = BODY_MODELS[request.operation].model_validate(request.body).model_dump()
        return request
    except (ValueError, TypeError, RecursionError):
        raise ValueError('INVALID_REQUEST') from None
