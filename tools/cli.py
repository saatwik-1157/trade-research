#!/usr/bin/env python3
"""One entry point for the toolkit, so it can ship as a single executable.

Every tool here already owns a `main()` and its own `argparse`. This dispatches
to them rather than re-declaring their flags, because a second copy of an
argument list is a second thing to keep in step, and it would drift.

    trade-research account --days 7
    trade-research paper --rule random --once
    trade-research rule-backtest --rule all --out reports/rb.json

`trade-research <command> --help` reaches the tool's own help, unchanged.

WHAT THIS IS NOT. None of these commands finds profitable trades, and nothing
in here should be read as claiming otherwise. The measurement commands exist to
tell you when a rule does NOT work, which is what they have said every time
they have been run: the composite scores an information coefficient of 0.002,
zero of 105 patterns survived multiple-testing correction, and a 41-candidate
search across ten rule families was beaten by its own shuffled signals. The
execution commands place orders on a DEMO account behind a code-level guard.
Both are honest instruments. Neither is an edge.
"""

from __future__ import annotations

import importlib
import os
import sys

# Frozen or not, the tools import one another flatly (`import rule_backtest`),
# so their own directory has to be importable as a top-level location.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

#: command -> (module, one-line summary). Grouped for the help text below.
TRADING: dict[str, tuple[str, str]] = {
    "account": ("mt5_account", "read a MetaTrader 5 account - read-only, never sends"),
    "paper": ("mt5_paper", "run a rule on a DEMO account; dry-run unless --live"),
    "harvest": ("take_profit", "close positions once they show a floating profit"),
    "overnight": ("run_overnight", "start a harvest session and stop it at a wall-clock hour"),
    "watchdog": ("watchdog", "restart a session that died, keeping its deadline"),
    "kill": ("kill_switch", "stop a running session without hunting for its process"),
    "crash": ("crash_report", "what a session was doing when it stopped"),
}

MEASUREMENT: dict[str, tuple[str, str]] = {
    "rule-backtest": ("rule_backtest", "replay the paper rules against MT5 history"),
    "rule-search": ("rule_search", "many rule families against a permutation null"),
    "exit-search": ("exit_search", "entry x exit combinations, including uncapped exits"),
    "bracket-sweep": ("bracket_sweep", "SL/TP grid with an out-of-sample holdout"),
    "cost-hurdle": ("cost_hurdle", "the win rate the spread demands before any strategy"),
    "cost-profile": ("cost_profile", "where the spread is cheapest: symbol, hour, timeframe"),
    "patterns": ("patterns", "event study over candlestick and calendar patterns"),
    "shape-search": ("shape_search", "candle-shape and volatility-regime rules"),
    "backtest": ("backtest", "does the composite score separate forward returns"),
    "swap": ("swap", "overnight financing, in points"),
    "carry-check": ("carry_check", "does a paid long side survive the spot drift"),
    "cross-search": ("cross_search", "dollar-neutral cross-sectional currency portfolios"),
    "autopsy": ("trade_autopsy", "which live trades made money, and which of that is real"),
    "calendar-search": ("calendar_search", "day-of-week, month and turn-of-month effects on FX"),
}

RECORDS: dict[str, tuple[str, str]] = {
    "track-record": ("track_record", "accumulating ledger of live paper trades"),
    "tv-import": ("tv_import", "import a TradingView trade export"),
    "tv-webhook": ("tv_webhook", "receive TradingView alerts; records, never trades"),
    "snapshot": ("snapshot", "build the numeric snapshot for a ticker"),
    "verify": ("verify", "check every number in a report traces back to its data"),
    "crypto": ("crypto_market", "crypto OHLCV in the shape rule-search consumes"),
    "state": ("project_state", "regenerate PROJECT_STATE.json by measuring"),
}

GROUPS = (
    ("Trading and sessions", TRADING),
    ("Measurement", MEASUREMENT),
    ("Records and data", RECORDS),
)

COMMANDS: dict[str, tuple[str, str]] = {**TRADING, **MEASUREMENT, **RECORDS}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        "trade-research - stock and FX research tooling where numbers are computed.",
        "",
        "usage: trade-research <command> [options]",
        "       trade-research <command> --help    the tool's own help, unchanged",
        "",
    ]
    for title, group in GROUPS:
        lines.append(f"{title}:")
        for name, (_module, summary) in group.items():
            lines.append(f"  {name:<{width}}  {summary}")
        lines.append("")
    lines += [
        "No command here finds profitable trades. The measurement commands exist to",
        "tell you when a rule does not work, and every one of them has said so far.",
        "The trading commands are fenced to DEMO accounts in code, not in prose.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if args[0] in ("-V", "--version", "version"):
        print("trade-research toolkit")
        return 0

    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        near = [c for c in COMMANDS if c.startswith(command[:3])]
        print(f"unknown command {command!r}", file=sys.stderr)
        if near:
            print(f"did you mean: {', '.join(sorted(near))}", file=sys.stderr)
        print("\n" + _usage(), file=sys.stderr)
        return 2

    module_name, _summary = COMMANDS[command]
    module = importlib.import_module(module_name)
    entry = getattr(module, "main", None)
    if entry is None:  # pragma: no cover - guarded by test_cli
        print(f"{module_name} has no main() to dispatch to", file=sys.stderr)
        return 2

    # The tool parses `sys.argv` itself. Rewriting argv[0] means its own
    # --help prints the command the user actually typed rather than the
    # executable's path.
    saved = sys.argv
    sys.argv = [f"trade-research {command}", *rest]
    try:
        result = entry()
    finally:
        sys.argv = saved
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
