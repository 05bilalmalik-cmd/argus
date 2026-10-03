"""The only machine-facing bridge route; no operator or generic API dispatch."""
from __future__ import annotations

import asyncio
import ipaddress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.bridge.protocol import MAX_REQUEST_BYTES, parse_request, refusal, validate_reply

router = APIRouter(prefix='/api/preparation-bridge', tags=['preparation-bridge'])


def _denied(reason: str, code: int) -> JSONResponse:
    return JSONResponse(refusal(reason), status_code=code)


@router.post('/call', include_in_schema=False)
async def bridge_call(request: Request) -> JSONResponse:
    service = getattr(request.app.state, 'preparation_bridge', None)
    if service is None:
        return _denied('DISABLED', 503)
    # This route is for a privately configured loopback machine client, not
    # browser JavaScript. Keep the application's Host/Origin middleware too.
    try:
        peer_is_local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        peer_is_local = False
    authorization = request.headers.getlist('authorization')
    if (not peer_is_local or len(authorization) != 1
            or not authorization[0].startswith('Bearer ')
            or len(authorization[0]) != 71
            or 'origin' in request.headers or 'sec-fetch-site' in request.headers):
        return _denied('UNAUTHENTICATED', 401)
    try:
        authenticated = await run_in_threadpool(service.authenticate, authorization[0][7:])
    except Exception:
        return _denied('INTEGRITY', 500)
    if authenticated is not True:
        return _denied('UNAUTHENTICATED', 401)
    # Authentication precedes even parsing the body or querying job state.
    if request.query_params or request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/json':
        return _denied('INVALID_REQUEST', 400)
    try:
        length = request.headers.get('content-length')
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_REQUEST_BYTES):
            return _denied('INVALID_REQUEST', 400)
        async with asyncio.timeout(5):
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                    return _denied('INVALID_REQUEST', 400)
                raw.extend(chunk)
        call = parse_request(bytes(raw))
    except (ValueError, TypeError, TimeoutError, RecursionError):
        return _denied('INVALID_REQUEST', 400)
    try:
        result = await run_in_threadpool(service.dispatch, call.operation, call.body)
        return JSONResponse(validate_reply(result))
    except Exception:
        # Never turn arbitrary exceptions/projections into permissive results.
        # Try to persist the integrity stop; refusal remains closed if storage
        # itself is unavailable. No private exception text reaches HTTP or MCP.
        try:
            await run_in_threadpool(service.dispatch, 'pause_preparation', {'reason_code': 'INTEGRITY'})
        except Exception:
            pass
        return _denied('INTEGRITY', 500)
