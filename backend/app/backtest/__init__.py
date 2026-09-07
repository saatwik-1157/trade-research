"""Backtesting: the tested simulator, wrapped rather than replaced.

`tools/rule_backtest.simulate` is the engine. It already implements next-bar
entry, loss-on-a-straddled-bar, spread charged once and no overlapping
positions, and the toolkit's own tests pin every one of them. This package
turns an `app.strategies` Strategy into the signal vector it consumes, and
turns its trades back into an equity curve, metrics and stored rows.

Nothing here can reach a broker: no module imports `app.brokers`, and a test
asserts that absence.
"""

from app.backtest.config import BacktestConfig, ConfigError, CostModel, SizingMode
from app.backtest.runner import BacktestError, BacktestResult, build_signal_vector, run
from app.backtest.service import BacktestBusy, BacktestService

__all__ = [
    "BacktestBusy",
    "BacktestConfig",
    "BacktestError",
    "BacktestResult",
    "BacktestService",
    "ConfigError",
    "CostModel",
    "SizingMode",
    "build_signal_vector",
    "run",
]
