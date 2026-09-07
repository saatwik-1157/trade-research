"""The execution-mode router: the one place a mode becomes an execution path.

This module is the level's safety boundary, and the shape of it is the point.

The prompt asks for a guard of the form::

    if execution_mode == PAPER:
        execution_provider = PAPER_EXECUTION_ONLY

A guard written that way can be wrong. It can be skipped, short-circuited by
an earlier branch, or handed a mode that some configuration mutated on the way
in. So the rule is not implemented as a check inside a longer function; it is
implemented as **a total function whose only parameter is the mode**:

    provider_for(ExecutionMode.paper) is ExecutionProvider.paper_execution_only

`provider_for` takes no settings, no account, no credentials and no override.
There is no parameter through which a configuration mistake could arrive, which
is what makes "configuration must never override this" true rather than
asserted. `PROVIDER_FOR_MODE` is a frozen mapping over the whole enum, and a
test walks every member so a mode added later cannot quietly fall through to a
default.

The second half of the boundary is `assert_paper_execution`, which the paper
execution provider calls on itself. It refuses anything that is not
`paper_execution_only`, so even a caller that obtained a provider by some other
route cannot use the paper path to reach a broker.

Nothing here imports `app.brokers`, `MetaTrader5` or `mt5_paper`, and a test
parses this package's modules to prove it. Paper execution cannot reach a venue
because there is no venue reference in the package to reach one with.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class ExecutionMode(StrEnum):
    """The four modes this platform distinguishes, and never blurs.

    BACKTEST  historical batch simulation
    REPLAY    historical interactive simulation
    PAPER     real/current market data, virtual money
    LIVE      real/current market data, a real broker
    """

    backtest = "backtest"
    replay = "replay"
    paper = "paper"
    live = "live"


class ExecutionProvider(StrEnum):
    batch_simulation = "BATCH_SIMULATION_ONLY"
    replay_simulation = "SIMULATED_EXECUTION_ONLY"
    paper_execution_only = "PAPER_EXECUTION_ONLY"
    live_broker_adapter = "LIVE_BROKER_ADAPTER"


# Total over ExecutionMode. A test asserts every member is present, so a mode
# added later fails loudly instead of falling through to a default -- and a
# default here would be a default execution path.
PROVIDER_FOR_MODE: MappingProxyType[ExecutionMode, ExecutionProvider] = MappingProxyType(
    {
        ExecutionMode.backtest: ExecutionProvider.batch_simulation,
        ExecutionMode.replay: ExecutionProvider.replay_simulation,
        ExecutionMode.paper: ExecutionProvider.paper_execution_only,
        ExecutionMode.live: ExecutionProvider.live_broker_adapter,
    }
)

# The three modes that can never reach a broker, whatever else is configured.
SIMULATED_MODES = frozenset({ExecutionMode.backtest, ExecutionMode.replay, ExecutionMode.paper})


class ExecutionRouteRefused(Exception):
    """A caller tried to use an execution path its mode does not authorise."""


def provider_for(mode: ExecutionMode) -> ExecutionProvider:
    """The execution provider for a mode. The mode is the only input.

    Deliberately not `provider_for(mode, settings)` and not
    `provider_for(mode, allow_live=...)`. A function with no configuration
    parameter cannot be overridden by configuration, and that is a stronger
    statement than any check inside one that could be.
    """
    return PROVIDER_FOR_MODE[ExecutionMode(mode)]


def assert_paper_execution(mode: ExecutionMode | str) -> ExecutionProvider:
    """Called by the paper execution provider on itself, before every fill.

    Refuses anything that does not route to `PAPER_EXECUTION_ONLY`, so the
    paper path cannot be borrowed by a live order even by a caller who
    constructed one directly.
    """
    resolved = provider_for(ExecutionMode(mode))
    if resolved is not ExecutionProvider.paper_execution_only:
        raise ExecutionRouteRefused(
            f"mode {mode} routes to {resolved}, not "
            f"{ExecutionProvider.paper_execution_only}; the paper execution "
            "provider refuses to fill an order it is not the provider for"
        )
    return resolved


def describe() -> dict[str, object]:
    """The boundary, in the API's own words."""
    return {
        "routes": {str(mode): str(provider) for mode, provider in PROVIDER_FOR_MODE.items()},
        "rule": (
            "The execution provider is a function of the mode and nothing else. "
            "`provider_for` accepts no settings, credentials or override, so there "
            "is no parameter through which configuration could change the route."
        ),
        "paper": (
            "PAPER always routes to PAPER_EXECUTION_ONLY. Live broker credentials "
            "being present changes nothing: the paper package holds no broker "
            "adapter and imports none, which a test proves by parsing every module."
        ),
        "live_trading_flag": (
            "LIVE_TRADING=true does NOT turn paper into live. It is a gate on the "
            "live route, never a promotion of another one."
        ),
    }
