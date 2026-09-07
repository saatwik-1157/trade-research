"""The Order Management System (L19).

**One state machine, one fill accounting, one set of rules.** `state.py` and
`fills.py` are shared by every execution mode; `service.OrderManager` binds
them to a `BrokerAdapter` (demo and live differ only by which adapter is
registered), and `app/paper/oms.py` binds the same two modules to the paper
venue's in-process execution provider. There is no `DemoOMS` and no `LiveOMS`.

The OMS decides nothing about whether to trade. It cannot: `create` requires a
risk `Approval`, and `Approval` is constructible only by `RiskEngine.approve`.
The quantity arrives on that approval already measured by `app.sizing`, and
this package does not import `app.sizing` at all -- recomputing a size here
would be a second authoritative sizing calculation.
"""

from app.oms.fills import FillBook, FillError, FillRecord
from app.oms.order import ManagedOrder, OrderTransition
from app.oms.service import (
    OrderManager,
    OrderRefused,
    ReconciliationRequired,
    Submission,
)
from app.oms.state import (
    NEEDS_RECONCILIATION,
    OPEN,
    SAFE_TO_RESEND,
    TERMINAL,
    TRANSITIONS,
    IllegalOrderTransition,
    OrderStatus,
    can_resend,
    check_transition,
    is_terminal,
)

__all__ = [
    "NEEDS_RECONCILIATION",
    "OPEN",
    "SAFE_TO_RESEND",
    "TERMINAL",
    "TRANSITIONS",
    "FillBook",
    "FillError",
    "FillRecord",
    "IllegalOrderTransition",
    "ManagedOrder",
    "OrderManager",
    "OrderRefused",
    "OrderStatus",
    "OrderTransition",
    "ReconciliationRequired",
    "Submission",
    "can_resend",
    "check_transition",
    "is_terminal",
]
