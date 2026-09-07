"""CSRF protection: double-submit cookie.

Why this is needed even with SameSite=Lax. Lax stops a cross-site POST from a
form or an image, which covers most of it, but it is a browser policy rather
than a check the server performs: an older browser, a same-site subdomain, or
a future relaxation of the cookie policy all bypass it silently. A token the
server verifies does not depend on the browser behaving.

The scheme: on sign-in the server sets a random token in a **readable**
cookie (not HttpOnly, by design — the page must be able to read it) and
requires the same value in the `X-CSRF-Token` header on every state-changing
request that carries a session. An attacker's page can cause the session
cookie to be sent, but same-origin policy stops it reading the token cookie,
so it cannot populate the header.

Exempt: `GET`, `HEAD`, `OPTIONS` (no state change), and requests with no
session cookie (nothing to ride on — sign-in itself is rate limited instead).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.core.errors import REQUEST_ID_HEADER, error_body, request_id_of

log = logging.getLogger("app.auth.csrf")

CSRF_COOKIE = "tr_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Endpoints that ESTABLISH a session rather than act on one. They are exempt
# because they do not use the current session's authority — they replace it —
# and requiring a token there would break signing in while an old cookie is
# still present, which is an ordinary thing to do. They are protected by rate
# limiting instead. The residual risk is login CSRF (forcing a victim into an
# attacker's session); it is accepted here and revisited at L39, because the
# alternative is refusing legitimate sign-ins.
EXEMPT_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/register",
        "/auth/password-reset/request",
        "/auth/password-reset/complete",
    }
)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, secure: bool, max_age: int) -> None:
    """Readable by the page on purpose: the page must echo it in a header."""
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(key=CSRF_COOKIE, path="/")


def make_middleware(
    session_cookie_name: str, enabled: bool = True
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def csrf_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not enabled or request.method in SAFE_METHODS:
            return await call_next(request)
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # No session means nothing to ride on. Sign-in and registration are
        # protected by rate limiting rather than by a token they cannot have.
        if not request.cookies.get(session_cookie_name):
            return await call_next(request)

        cookie = request.cookies.get(CSRF_COOKIE) or ""
        header = request.headers.get(CSRF_HEADER) or ""
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            rid = request_id_of(request)
            log.warning(
                "csrf check failed",
                extra={
                    "event": "csrf_failed",
                    "path": request.url.path,
                    "method": request.method,
                    "request_id": rid,
                    "had_cookie": bool(cookie),
                    "had_header": bool(header),
                },
            )
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "csrf_failed",
                    "missing or mismatched CSRF token; send the tr_csrf cookie value "
                    "in the X-CSRF-Token header",
                    rid,
                ),
                headers={REQUEST_ID_HEADER: rid},
            )
        return await call_next(request)

    return csrf_middleware
