"""Controlled live-trading activation (L70).

Four pieces, and every one of them can only ever REFUSE:

    allowlist.py   which account identifiers may trade live. Empty permits none.
    gate.py        LiveTradingGate -- the mandatory checks and their verdicts.
    state.py       the activation state machine and its legal transitions.
    preflight.py   `python -m app.live.preflight`, which prints the report.

**Nothing in this package can place, modify, cancel or size an order.** It
imports no adapter, no order manager and no risk engine, and
`test_live_gate.py` parses every module here to prove it. That is the whole
design: an activation layer that could trade would be a second execution path,
and the one thing this platform does not need is a second execution path.

The order of authority is unchanged by anything here:

    LiveTradingGate says whether live execution may be TURNED ON.
    RiskEngine says whether any individual order may be SENT.

The gate is not a substitute for the engine and never runs in its place. An
order still needs an `Approval` that only `RiskEngine.approve` can mint, and
the OMS is still the only path to a venue.
"""

from app.live.allowlist import Allowlist
from app.live.gate import Check, GateReport, LiveContext, LiveTradingGate, Verdict
from app.live.state import (
    ActivationMachine,
    ActivationState,
    IllegalActivationTransition,
    Transition,
)

__all__ = [
    "ActivationMachine",
    "ActivationState",
    "Allowlist",
    "Check",
    "GateReport",
    "IllegalActivationTransition",
    "LiveContext",
    "LiveTradingGate",
    "Transition",
    "Verdict",
]
