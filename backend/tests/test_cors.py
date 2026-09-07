from __future__ import annotations

from app.core.settings import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_dev_ui_origin_is_allowed(settings: Settings) -> None:
    app = create_app(settings, checks={})
    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": "http://127.0.0.1:3000"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"


def test_unknown_origin_gets_no_cors_header(settings: Settings) -> None:
    app = create_app(settings, checks={})
    with TestClient(app) as client:
        r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
