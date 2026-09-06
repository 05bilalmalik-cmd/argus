"""Private validation of the legacy dual-purpose AutomationRun.receipt_json.

A nonempty value is not itself a submission receipt: PREFILL writes a field
manifest here. Permit only that producer's closed preparation schema. Actual
receipts, unknown keys, malformed JSON and unconfirmed 'filled' claims fail closed.
No data from this module is projected to the MCP client.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.bridge.protocol import ClosedModel, load_json


class _PreparationField(ClosedModel):
    label: str = Field(max_length=200)
    canonical_key: str = Field(max_length=128)
    outcome: Literal['filled', 'failed', 'deferred_to_human', 'blank', 'unknown']
    required: bool
    reason_code: str = Field(max_length=1024)
    readback_confirmed: bool

    @model_validator(mode='after')
    def verified_claim(self):
        if self.readback_confirmed is not (self.outcome == 'filled'):
            raise ValueError('INTEGRITY')
        return self


class _PreparationManifest(ClosedModel):
    mode: Literal['prefill']
    state: Literal['NEEDS_USER', 'READY_TO_SUBMIT', 'FINAL_REVIEW', 'HUMAN_REQUIRED',
                   'NEEDS_OA', 'ACTIVE', 'BLOCKED', 'FAILED']
    adapter: str = Field(max_length=128)
    risk_level: int = Field(ge=0, le=4)
    blocked_reasons: list[str] = Field(max_length=2000)
    fields: list[_PreparationField] = Field(default_factory=list, max_length=2000)


def unsafe_persisted_receipt(raw: str | None) -> bool:
    # Legacy empty audit storage is absence of receipt evidence, NOT evidence
    # of successful filling. Core still requires the matching run and live owner.
    if raw in (None, '', '{}', 'null'):
        return False
    if not isinstance(raw, str):
        return True
    try:
        _PreparationManifest.model_validate(load_json(raw.encode('utf-8'), maximum=1024 * 1024))
    except (ValueError, TypeError, RecursionError, OverflowError):
        return True
    return False
