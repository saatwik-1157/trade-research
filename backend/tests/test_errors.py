"""Error handling: one shape, a request id, and nothing leaked."""

from __future__ import annotations

from app.core.errors import (
    REQUEST_ID_HEADER,
    AppError,
    Conflict,
    NotFound,
    NotImplementedYet,
)
from app.core.settings import Settings
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


def app_with_routes(settings: Settings) -> FastAPI:
    app = create_app(settings, checks={})

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2 and the host is db.internal")

    @app.get("/missing")
    async def missing() -> None:
        raise NotFound("no such widget")

    @app.get("/clash")
    async def clash() -> None:
        raise Conflict("already exists")

    @app.get("/later")
    async def later() -> None:
        raise NotImplementedYet(19, "order routing")

    return app


def test_every_response_carries_a_request_id(settings: Settings) -> None:
    with TestClient(app_with_routes(settings)) as client:
        ok = client.get("/health")
        assert REQUEST_ID_HEADER in ok.headers
        assert len(ok.headers[REQUEST_ID_HEADER]) >= 8


def test_a_caller_supplied_request_id_is_echoed(settings: Settings) -> None:
    with TestClient(app_with_routes(settings)) as client:
        r = client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert r.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_an_oversized_caller_id_is_capped(settings: Settings) -> None:
    with TestClient(app_with_routes(settings)) as client:
        r = client.get("/health", headers={REQUEST_ID_HEADER: "x" * 500})
    assert len(r.headers[REQUEST_ID_HEADER]) == 64


def test_unhandled_exceptions_never_leak_internals(settings: Settings) -> None:
    client = TestClient(app_with_routes(settings), raise_server_exceptions=False)
    with client:
        r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error"
    assert body["request_id"] == r.headers[REQUEST_ID_HEADER]
    # The message, the password and the host all stay in the log.
    assert "hunter2" not in r.text
    assert "db.internal" not in r.text
    assert "Traceback" not in r.text and "RuntimeError" not in r.text


def test_domain_errors_map_to_their_status_and_code(settings: Settings) -> None:
    with TestClient(app_with_routes(settings)) as client:
        missing = client.get("/missing")
        clash = client.get("/clash")
        later = client.get("/later")

    assert missing.status_code == 404
    assert missing.json()["error"] == {
        "code": "not_found",
        "detail": "no such widget",
        "request_id": missing.headers[REQUEST_ID_HEADER],
    }
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "conflict"
    assert later.status_code == 501
    assert "level 19" in later.json()["error"]["detail"]


def test_validation_errors_report_fields_but_not_values(settings: Settings) -> None:
    with TestClient(app_with_routes(settings)) as client:
        r = client.post(
            "/auth/login", json={"email": "not-an-email", "password": "s3cret-value-here"}
        )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_failed"
    assert any("email" in f["loc"] for f in body["fields"])
    # The submitted password must not come back in the error.
    assert "s3cret-value-here" not in r.text


def test_http_exceptions_use_the_same_envelope(settings: Settings) -> None:
    with TestClient(app_with_routes(settings)) as client:
        r = client.get("/auth/me")  # no session
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthenticated"
    assert body["request_id"]


def test_app_error_defaults_and_custom_code() -> None:
    assert AppError("x").status_code == 400
    assert AppError("x", code="custom").code == "custom"
    assert NotImplementedYet(7).level == 7
