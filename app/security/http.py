from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Permitted-Cross-Domain-Policies": "none",
}


def _trusted_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalised = hostname.casefold().rstrip(".")
    if normalised in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(normalised).is_loopback
    except ValueError:
        return False


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme.casefold())


def _same_origin(origin: str, *, scheme: str, hostname: str | None, port: int | None) -> bool:
    parsed = urlsplit(origin)
    if not parsed.scheme or not parsed.hostname:
        return False
    expected_port = port or _default_port(scheme)
    actual_port = parsed.port or _default_port(parsed.scheme)
    return (
        parsed.scheme.casefold() == scheme.casefold()
        and parsed.hostname.casefold().rstrip(".") == (hostname or "").casefold().rstrip(".")
        and actual_port == expected_port
    )


class LocalSecurityMiddleware:
    """Protect the loopback UI from DNS rebinding and cross-site state changes."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        hostname = urlsplit(f"//{host_header}").hostname
        if not _trusted_hostname(hostname):
            await JSONResponse({"detail": "Untrusted Host header"}, status_code=400)(
                scope, receive, self._secure_send(send)
            )
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in _UNSAFE_METHODS and path != "/api/capture":
            fetch_site = headers.get("sec-fetch-site", "").casefold()
            origin = headers.get("origin")
            scheme = str(scope.get("scheme", "http"))
            server = scope.get("server")
            request_port = server[1] if server else None
            origin_is_null = origin is not None and origin.casefold() == "null"
            cross_site = fetch_site == "cross-site" or bool(
                origin
                and not origin_is_null
                and not _same_origin(
                    origin,
                    scheme=scheme,
                    hostname=hostname,
                    port=request_port,
                )
            )
            if origin_is_null and fetch_site not in {"same-origin", "same-site"}:
                cross_site = True
            if cross_site:
                await JSONResponse(
                    {"detail": "Cross-site state change refused"}, status_code=403
                )(scope, receive, self._secure_send(send))
                return

        await self.app(scope, receive, self._secure_send(send))

    @staticmethod
    def _secure_send(send: Send) -> Callable[[Message], Awaitable[None]]:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    response_headers[name] = value
            await send(message)

        return wrapped
