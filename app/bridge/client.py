"""Private HTTP facade; no candidate database, browser, or application imports."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import Field, field_validator

from app.bridge.protocol import (
    MAX_REPLY_BYTES, ClosedModel, load_json, parse_request, refusal, validate_reply,
)


class _Credentials(ClosedModel):
    endpoint: str = Field(repr=False)
    token: str = Field(repr=False)
    expires_at: Annotated[int | float, Field(repr=False)]

    @field_validator('endpoint')
    @classmethod
    def exact_loopback(cls, value: str) -> str:
        match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})', value)
        if match is None or not 1 <= int(match[1]) <= 65535:
            raise ValueError('TRANSPORT_ERROR')
        return value

    @field_validator('token')
    @classmethod
    def hex_token(cls, value: str) -> str:
        # Operator supplies random bytes as hex (128..512 bits). Entropy is
        # the operator's responsibility; a reader cannot prove randomness.
        if re.fullmatch(r'[0-9a-fA-F]{32,128}', value) is None or len(value) % 2:
            raise ValueError('TRANSPORT_ERROR')
        return value

    @field_validator('expires_at')
    @classmethod
    def future_expiry(cls, value: int | float) -> int | float:
        if not math.isfinite(value) or value <= time.time():
            raise ValueError('TRANSPORT_ERROR')
        return value


class BridgeClient:
    def __init__(self, credential_file: Path):
        self._credential_file = credential_file

    def call(self, operation: str, body: dict) -> dict:
        try:
            request = parse_request(json.dumps(
                {'operation': operation, 'body': body}, allow_nan=False,
            ).encode('utf-8'))
        except (ValueError, TypeError, RecursionError, OverflowError):
            return refusal('INVALID_REQUEST')
        try:
            # Re-read privately per call so deletion/rotation/expiry take effect.
            with self._credential_file.open('rb') as source:
                credentials = _Credentials.model_validate(load_json(source.read(16385), maximum=16384))
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=120.0) as client:
                with client.stream(
                    'POST', credentials.endpoint + '/api/preparation-bridge/call',
                    headers={'Authorization': 'Bearer ' + credentials.token,
                             'Accept': 'application/json', 'Accept-Encoding': 'identity'},
                    json=request.model_dump(),
                ) as response:
                    if response.status_code != 200:
                        return refusal('TRANSPORT_ERROR')
                    if response.headers.get('content-encoding', 'identity') != 'identity':
                        return refusal('TRANSPORT_ERROR')
                    if response.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                        return refusal('TRANSPORT_ERROR')
                    declared = response.headers.get('content-length')
                    if declared is not None and (not declared.isascii() or not declared.isdecimal()
                                                 or int(declared) > MAX_REPLY_BYTES):
                        return refusal('TRANSPORT_ERROR')
                    raw = bytearray()
                    # One-byte delivery prevents the chunker waiting for another
                    # full block after the first byte beyond the hard limit.
                    for chunk in response.iter_raw(chunk_size=1):
                        if len(raw) + len(chunk) > MAX_REPLY_BYTES:
                            return refusal('TRANSPORT_ERROR')
                        raw.extend(chunk)
                    return validate_reply(load_json(bytes(raw), maximum=MAX_REPLY_BYTES))
        except (httpx.HTTPError, OSError, ValueError, TypeError, RecursionError, OverflowError):
            # No retries: a lost response may follow a successful preparation.
            # Neither exception text, error bodies nor private config are emitted.
            return refusal('TRANSPORT_ERROR')
