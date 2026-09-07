"""Position sizing (L18).

`calculator` is the engine and is pure. `service` adds counters and logging.
Nothing in this package imports `app.brokers`, `app.paper` or `app.risk`:
sizing proposes a quantity, and the authority to trade it lives elsewhere.
`test_sizing.py::test_sizing_holds_no_execution_authority` asserts it.
"""

from app.sizing.calculator import (
    METHOD_ALIASES,
    RISK_METHODS,
    SizingError,
    SizingMethod,
    SizingRequest,
    SizingResult,
    SizingStatus,
    calculate,
    resolve_method,
)
from app.sizing.service import SizingService

__all__ = [
    "METHOD_ALIASES",
    "RISK_METHODS",
    "SizingError",
    "SizingMethod",
    "SizingRequest",
    "SizingResult",
    "SizingService",
    "SizingStatus",
    "calculate",
    "resolve_method",
]
