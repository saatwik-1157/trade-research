"""Error handling and request correlation.

Three things, all foundation-level:

  * one exception hierarchy the whole application raises,
  * one response shape every failure produces,
  * a request id on every request, response and log line.

The response shape carries a `code` and a `request_id` and never carries a
stack trace, a database URL or a driver message. An unhandled exception is
logged in full and answered with the id alone, so the operator can find the
log line without the client being handed the internals.

Domain errors that already exist elsewhere (`app.symbols.errors.SymbolError`,
`app.auth.service.AuthError`) are adapted here rather than rewritten: both
already carry a `status_code`, so they are mapped, not replaced.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.symbols.errors import SymbolError

log = logging.getLogger("app.errors")

REQUEST_ID_HEADER = "X-Request-ID"

# Stable machine-readable codes for the symbol layer's refusals. The class name
# is the source of truth; this maps it to a code a client can branch on without
# parsing prose.
_SYMBOL_CODES = {
    "InvalidSymbolCode": "invalid_symbol_code",
    "UnknownSymbol": "unknown_symbol",
    "UnknownSourceSymbol": "unmapped_provider_symbol",
    "AmbiguousSourceSymbol": "ambiguous_provider_symbol",
    "NoProviderMapping": "no_provider_mapping",
    "DuplicateMapping": "duplicate_mapping",
    "SymbolInactive": "symbol_inactive",
    "IncompleteContractSpec": "incomplete_contract_spec",
}


class AppError(Exception):
    """Base for every error this application raises deliberately."""

    status_code = 400
    code = "app_error"

    def __init__(self, detail: str, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class NotImplementedYet(AppError):
    """A route that exists so authorization can be enforced before it works."""

    status_code = 501
    code = "not_implemented"

    def __init__(self, level: int, what: str = "") -> None:
        super().__init__(
            f"not built until level {level:02d}; nothing to return" + (f" ({what})" if what else "")
        )
        self.level = level


class DependencyUnavailable(AppError):
    """A dependency the request needed could not be reached."""

    status_code = 503
    code = "dependency_unavailable"


def request_id_of(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, str) else "-"


def error_body(code: str, detail: str, request_id: str, **extra: object) -> dict:
    return {"error": {"code": code, "detail": detail, "request_id": request_id, **extra}}


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Accept a caller's id or mint one, and echo it on the response.

    A caller-supplied id is length-capped and used as-is so a trace can span
    services; it is only ever logged and echoed, never interpreted.
    """
    incoming = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    request_id = incoming[:64] if incoming else uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def install(app: FastAPI) -> None:
    """Register the middleware and the handlers. Idempotent per app."""
    app.middleware("http")(request_id_middleware)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = request_id_of(request)
        log.warning(
            "handled application error",
            extra={
                "event": "app_error",
                "code": exc.code,
                "status": exc.status_code,
                "request_id": rid,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.detail, rid),
            headers={REQUEST_ID_HEADER: rid},
        )

    @app.exception_handler(SymbolError)
    async def _symbol(request: Request, exc: SymbolError) -> JSONResponse:
        # This handler is what the module docstring above has always claimed:
        # SymbolError carries its own status_code, so it is mapped rather than
        # rewritten. It was unreachable until L06 gave the symbol layer routes,
        # and an unmapped SymbolError answers 500 -- which would report a
        # refusal the layer made deliberately as a fault in the server.
        rid = request_id_of(request)
        code = _SYMBOL_CODES.get(type(exc).__name__, "symbol_error")
        log.warning(
            "symbol resolution refused",
            extra={
                "event": "symbol_error",
                "code": code,
                "status": exc.status_code,
                "request_id": rid,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc), rid),
            headers={REQUEST_ID_HEADER: rid},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = request_id_of(request)
        # Field names and messages only. The submitted values are not echoed:
        # a rejected login body carries a password.
        fields = [
            {"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_failed", "request failed validation", rid, fields=fields
            ),
            headers={REQUEST_ID_HEADER: rid},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        rid = request_id_of(request)
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        code = {401: "unauthenticated", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, detail, rid),
            headers={REQUEST_ID_HEADER: rid},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = request_id_of(request)
        # Full detail to the log, the id alone to the client.
        log.exception(
            "unhandled exception",
            extra={"event": "unhandled_exception", "request_id": rid, "path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content=error_body(
                "internal_error",
                "an internal error occurred; quote the request id when reporting it",
                rid,
            ),
            headers={REQUEST_ID_HEADER: rid},
        )
