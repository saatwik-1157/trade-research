from __future__ import annotations

import io
import json
import logging

from app.core.logging import JsonFormatter, configure_logging


def _capture(record_fn) -> dict:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    lg = logging.getLogger("test.json")
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    record_fn(lg)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_json_line_has_core_fields() -> None:
    row = _capture(lambda lg: lg.info("hello %s", "world"))
    assert row["message"] == "hello world"
    assert row["level"] == "INFO"
    assert row["logger"] == "test.json"
    assert row["ts"].endswith("+00:00")


def test_extra_fields_become_top_level_keys() -> None:
    row = _capture(lambda lg: lg.info("order", extra={"intent_id": "abc", "trading_mode": "paper"}))
    assert row["intent_id"] == "abc"
    assert row["trading_mode"] == "paper"


def test_exception_is_included() -> None:
    def go(lg: logging.Logger) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            lg.exception("failed")

    row = _capture(go)
    assert "ValueError: boom" in row["exc_info"]


def test_configure_is_idempotent() -> None:
    root = configure_logging("DEBUG", json_lines=True)
    configure_logging("INFO", json_lines=True)
    tagged = [h for h in root.handlers if getattr(h, "trade_research_json_handler", False)]
    assert len(tagged) == 1
    assert root.level == logging.INFO
