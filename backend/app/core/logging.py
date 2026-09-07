"""Structured logging.

One JSON object per line on stdout, so a worker's output can be shipped and
queried without a parser per module. Extra fields passed via ``extra=`` land
as top-level keys, which is how later levels attach ``intent_id``,
``signal_id`` and ``trading_mode`` to every execution record.

Nothing here reads a URL or a secret. Settings objects are never logged whole.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord carries; anything else came from ``extra=``.
_STANDARD_ATTRS = set(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

_HANDLER_TAG = "trade_research_json_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_lines: bool = True) -> logging.Logger:
    """Install one stdout handler on the root logger. Idempotent."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_TAG, True)
    if json_lines:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Route uvicorn through the same handler so the API's access and error
    # lines are the same shape as everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    return root
