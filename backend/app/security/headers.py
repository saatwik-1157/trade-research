"""Response security headers, and the one CORS rule that must not be got wrong.

Steps 21, 23 and 24.

**The audit found no security headers at all.** Not a weak policy -- none. This
adds them, and each one is chosen against the deployment this platform actually
has rather than copied from a list:

    Content-Security-Policy         the API serves JSON, so `default-src 'none'`
                                    is correct and total. A JSON response has no
                                    scripts, no styles, no images and no frames,
                                    and saying so costs nothing and closes the
                                    one class of attack that turns a reflected
                                    string into script.
    X-Content-Type-Options          nosniff. Without it a browser may decide a
                                    JSON error body is HTML.
    X-Frame-Options / frame-ancestors
                                    DENY. Nothing here is meant to be framed,
                                    and a framed trading UI is a clickjacked one.
    Referrer-Policy                 no-referrer. A request id or an account id in
                                    a path must not travel to a third party in a
                                    Referer header.
    Permissions-Policy              the browser features this API will never use,
                                    denied outright.
    Cross-Origin-Opener-Policy      same-origin, so an opener cannot reach into
                                    the window.
    Strict-Transport-Security       PRODUCTION ONLY, and see below.

**HSTS is not sent in development, and that is deliberate.** Sending it from a
`http://127.0.0.1` development server pins the browser to HTTPS for that host
for a year, which breaks every other local project on the same loopback address
and is remembered long after this one is deleted. It is emitted only when
`ENVIRONMENT=production`, where TLS is terminated at the proxy.

**The CSP is for the API's own responses.** The Next.js frontend is a separate
origin (or a separate path behind nginx) and needs a policy that permits its own
scripts; that belongs to the proxy or the frontend, and `nginx/nginx.conf` is
where it goes. Applying an API policy to a rendered page would break the page,
which step 23 says not to do.

**One CORS rule is enforced rather than documented.** `allow_credentials=True`
with a wildcard origin is the combination that turns any site into an
authenticated caller of this API, and Starlette will happily reflect the
requesting origin back. The settings validator refuses it at startup -- see
`app.core.settings`. This module keeps the header list explicit for the same
reason: `allow_headers=["*"]` with credentials is wider than anything the
frontend asks for.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import MutableHeaders

from app.auth.csrf import CSRF_HEADER

#: One year, and only ever in production. `includeSubDomains` is deliberately
#: absent: this platform does not know what else lives on the parent domain,
#: and pinning a sibling service to HTTPS is not its decision to make.
HSTS = "max-age=31536000"

#: The API answers JSON. Nothing it returns should be able to load anything.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

#: Browser features this API will never need. Denied rather than left to the
#: browser's default, which changes between versions.
PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
    "encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
)

BASE_HEADERS: dict[str, str] = {
    "Content-Security-Policy": API_CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # Belt and braces on top of the error envelope: nothing here should ever be
    # cached by a shared proxy, because much of it is one user's own data.
    "Cache-Control": "no-store",
}

#: The headers the frontend is allowed to send. Explicit rather than `*`,
#: because `*` with credentials is wider than anything the client asks for.
ALLOWED_REQUEST_HEADERS: list[str] = [
    "Accept",
    "Accept-Language",
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    CSRF_HEADER,
    "X-Request-ID",
]


class SecurityHeadersMiddleware:
    """Attach the headers to every response, including error responses.

    **Pure ASGI, deliberately, and not `app.middleware("http")`.** The obvious
    implementation is a `BaseHTTPMiddleware` dispatch function, and that was the
    first one written here. It was replaced after measurement: adding a second
    `BaseHTTPMiddleware` to this application roughly DOUBLED the intermittent
    failures in `tests/test_realtime.py`'s WebSocket tests (8 flaky failures
    against 2 with it removed, over the same module).

    `BaseHTTPMiddleware` wraps each request in an anyio task group and pumps the
    response through a memory stream. That is a lot of machinery for "add nine
    headers", and this application already has two of them (request-id and
    CSRF). A pure ASGI middleware adds no task, no stream and no scheduling
    point, and it can ignore non-HTTP scopes outright -- a WebSocket handshake
    never enters it at all.

    Headers are set on the `http.response.start` message, which is why this
    catches a 401 from the auth gate, a 403 from the CSRF wall and a 500 from
    the error handler: those are the responses an attacker sees most, and a
    dependency-based implementation would miss every one of them.
    """

    def __init__(self, app: Any, settings: Any) -> None:
        self.app = app
        self.production = settings.environment.value == "production"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # WebSocket and lifespan pass straight through. There is no header
            # to add to a handshake this middleware should be touching.
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in BASE_HEADERS.items():
                    # A route that deliberately set its own policy keeps it.
                    # Nothing does today, and a later one might.
                    if name not in headers:
                        headers[name] = value
                if self.production and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = HSTS
            await send(message)

        await self.app(scope, receive, send_with_headers)


def describe(settings: Any) -> dict[str, Any]:
    """What is sent, and what is deliberately not. For the security status."""
    production = settings.environment.value == "production"
    return {
        "headers": dict(BASE_HEADERS),
        "hsts": HSTS if production else None,
        "hsts_note": (
            "production only. Sending it from a development server on 127.0.0.1 "
            "pins the browser to HTTPS for that host for a year and breaks every "
            "other local project on the same address."
        ),
        "csp_scope": (
            "these are the API's own responses, which are JSON. The rendered "
            "frontend needs a policy that permits its own scripts; that belongs "
            "to the proxy, not here."
        ),
        "cors": {
            "allowed_origins": list(settings.cors_origins),
            "credentials": True,
            "allowed_headers": list(ALLOWED_REQUEST_HEADERS),
            "wildcard_refused": (
                "a wildcard origin with credentials is refused at startup: it "
                "would make any site an authenticated caller of this API."
            ),
        },
    }


__all__ = [
    "ALLOWED_REQUEST_HEADERS",
    "API_CSP",
    "BASE_HEADERS",
    "HSTS",
    "PERMISSIONS_POLICY",
    "SecurityHeadersMiddleware",
    "describe",
]
