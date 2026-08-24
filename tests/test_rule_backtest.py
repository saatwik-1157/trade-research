"""Checks on the rule backtester, run with plain python - no test framework.

    python tests/test_rule_backtest.py

A backtest is only worth the constraints it refuses to relax. Three of them are
load-bearing here and none is visible by reading an equity curve:

  * The backtest's indicators must be the ones the live tool trades. If
    `wilder_rsi` drifts from `mt5_paper.rule_rsi_reversion`, the measurement
    stops describing the thing being measured and nothing downstream notices.
  * Entry must come from closed bars only. Reading the bar you enter on is the
    cheapest way to manufacture an edge that does not exist.
  * A bar covering both stop and target must book the loss. Intrabar order is
    unknown, and resolving the ambiguity in the strategy's favour is the
    classic way a backtest flatters itself.

Also pinned: the order fill-mode mapping in `mt5_paper.filling_for`. The symbol
bitmask (FOK=1, IOC=2) and the order constants (FOK=0, IOC=1, RETURN=2) are
different enumerations, and conflating them sends IOC to a FOK-only symbol,
which the server rejects with retcode 10030. That was a live bug: every --live
order was refused until it was fixed, so it gets a test.

No network and no MetaTrader5 package required - both modules import the MT5
package inside connect() rather than at module scope.
"""
from __future__ import annotations

import os
import sys
import types

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mt5_paper  # noqa: E402
import rule_backtest as rb  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<56} got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(name)


def close_to(name: str, got, want, tol):
    ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<56} got={got!r} want~{want!r} tol={tol}")
    if not ok:
        FAILURES.append(name)


def synthetic_rates(n=400, seed=7):
    """A deterministic OHLC series with enough movement to trigger both rules."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.25, n))
    high = close + np.abs(rng.normal(0, 0.15, n))
    low = close - np.abs(rng.normal(0, 0.15, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return np.rec.fromarrays([open_, high, low, close],
                             names="open,high,low,close")


# --------------------------------------------------------------- fill mode
def fake_mt5():
    m = types.SimpleNamespace()
    m.ORDER_FILLING_FOK, m.ORDER_FILLING_IOC, m.ORDER_FILLING_RETURN = 0, 1, 2
    return m


def test_filling_mode():
    print("\nOrder fill mode - symbol bitmask must be translated, not reused")
    m = fake_mt5()

    # SYMBOL_FILLING_FOK = 1. The pre-fix code returned ORDER_FILLING_IOC (1)
    # here purely because the bit and the constant share the value 1.
    check("FOK-only symbol (mask 1) -> ORDER_FILLING_FOK",
          mt5_paper.filling_for(m, types.SimpleNamespace(filling_mode=1)), 0)
    check("IOC-only symbol (mask 2) -> ORDER_FILLING_IOC",
          mt5_paper.filling_for(m, types.SimpleNamespace(filling_mode=2)), 1)
    check("FOK+IOC symbol (mask 3) -> ORDER_FILLING_FOK",
          mt5_paper.filling_for(m, types.SimpleNamespace(filling_mode=3)), 0)
    check("symbol advertising neither (mask 0) -> ORDER_FILLING_RETURN",
          mt5_paper.filling_for(m, types.SimpleNamespace(filling_mode=0)), 2)
    check("symbol_info without the attribute -> ORDER_FILLING_RETURN",
          mt5_paper.filling_for(m, types.SimpleNamespace()), 2)


# ------------------------------------------------------- indicator agreement
def test_indicators_match_live():
    print("\nBacktest indicators must equal the ones mt5_paper trades")
    rates = synthetic_rates()

    # ATR: atr_from drops the forming bar itself, so compare against the
    # series computed on that same closed-bar slice.
    live_atr = mt5_paper.atr_from(rates)
    series = rb.atr_series(rates["high"][:-1], rates["low"][:-1], rates["close"][:-1])
    close_to("atr_series[-1] == mt5_paper.atr_from", float(series[-1]), float(live_atr), 1e-12)

    # RSI on an identical window must agree exactly - same seed, same loop.
    c = rates["close"][:-1]
    d = np.diff(c)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag, al = gain[:14].mean(), loss[:14].mean()
    for g, l in zip(gain[14:], loss[14:]):
        ag = (ag * 13 + g) / 14
        al = (al * 13 + l) / 14
    live_rsi = 100 - 100 / (1 + ag / al)
    close_to("wilder_rsi[-1] == mt5_paper's RSI on same window",
             float(rb.wilder_rsi(c)[-1]), float(live_rsi), 1e-9)

    # The documented claim: seeding from a 300-bar window rather than the full
    # series changes nothing that matters, because the seed decays as
    # (13/14)^k. If that ever stops holding, the backtest is measuring a
    # different signal from the one that trades.
    full = rb.wilder_rsi(rates["close"])
    k = len(rates) - 1
    window = rates["close"][k - 299:k + 1]
    dw = np.diff(window)
    gw = np.where(dw > 0, dw, 0.0)
    lw = np.where(dw < 0, -dw, 0.0)
    ag, al = gw[:14].mean(), lw[:14].mean()
    for g, l in zip(gw[14:], lw[14:]):
        ag = (ag * 13 + g) / 14
        al = (al * 13 + l) / 14
    windowed = 100 - 100 / (1 + ag / al)
    close_to("full-series RSI == 300-bar-window RSI (seed decay)",
             float(full[k]), float(windowed), 1e-6)


# --------------------------------------------------------------- simulation
def flat_market(n=100, price=100.0):
    o = np.full(n, price)
    h = np.full(n, price + 0.1)
    l = np.full(n, price - 0.1)
    c = np.full(n, price)
    return o, h, l, c


def test_entry_uses_closed_bars_only():
    print("\nEntry timing - a signal on bar i-1 is actionable at bar i's open")
    o, h, l, c = flat_market()
    o[65] = 123.0                      # make the entry bar's open unmistakable
    atr = np.full(100, 1.0)
    sig = np.zeros(100, dtype=int)
    sig[64] = 1                        # signal read from bar 64 (closed)

    trades = rb.simulate(o, h, l, c, sig, atr, spread=0.0)
    check("one trade opened", len(trades), 1)
    if trades:
        check("entry index is the bar after the signal", trades[0]["entry_idx"], 65)


def test_straddled_bar_books_the_loss():
    print("\nAmbiguous bar - covering both stop and target must resolve as a loss")
    o, h, l, c = flat_market()
    atr = np.full(100, 1.0)
    sig = np.zeros(100, dtype=int)
    sig[64] = 1                        # buy at o[65] = 100, sl 98.5, tp 101.5
    h[65], l[65] = 102.0, 98.0         # this single bar reaches both

    trades = rb.simulate(o, h, l, c, sig, atr, spread=0.0)
    check("trade recorded", len(trades), 1)
    if trades:
        check("exit reason is the stop, not the target", trades[0]["reason"], "sl")
        close_to("loss booked at the stop distance", trades[0]["gross"], -1.5, 1e-9)

    # And the mirror case for a short, where the stop sits above entry.
    o2, h2, l2, c2 = flat_market()
    sig2 = np.zeros(100, dtype=int)
    sig2[64] = -1                      # sell at 100, sl 101.5, tp 98.5
    h2[65], l2[65] = 102.0, 98.0
    t2 = rb.simulate(o2, h2, l2, c2, sig2, atr, spread=0.0)
    if t2:
        check("short side also books the stop", t2[0]["reason"], "sl")
        close_to("short loss booked at the stop distance", t2[0]["gross"], -1.5, 1e-9)


def test_spread_is_charged_once_per_trade():
    print("\nCosts - the spread must reduce net, and only net")
    o, h, l, c = flat_market()
    atr = np.full(100, 1.0)
    sig = np.zeros(100, dtype=int)
    sig[64] = 1
    h[65], l[65] = 102.0, 98.0

    free = rb.simulate(o, h, l, c, sig, atr, spread=0.0)
    costed = rb.simulate(o, h, l, c, sig, atr, spread=0.4)
    if free and costed:
        close_to("gross is unchanged by spread", costed[0]["gross"], free[0]["gross"], 1e-12)
        close_to("net is gross minus one spread", costed[0]["net"],
                 free[0]["gross"] - 0.4, 1e-12)


def test_no_overlapping_positions():
    print("\nExposure - the simulator stays flat between trades")
    o, h, l, c = flat_market()
    atr = np.full(100, 1.0)
    sig = np.ones(100, dtype=int)       # signal on every single bar
    # Let every trade resolve on its entry bar so exits are unambiguous.
    h[:], l[:] = 102.0, 98.0

    trades = rb.simulate(o, h, l, c, sig, atr, spread=0.0)
    idx = [t["entry_idx"] for t in trades]
    check("a signal on every bar still yields non-overlapping entries",
          all(b > a for a, b in zip(idx, idx[1:])), True)
    check("entries are never on consecutive bars while a trade is open",
          all(b - a >= 1 for a, b in zip(idx, idx[1:])), True)


# ------------------------------------------------------------ history fetch
def test_fetch_rates_steps_down():
    print("\nHistory fetch - an over-large request must not be read as 'no data'")

    class Terminal:
        """Rejects anything above `cap`, the way the real terminal does."""
        TIMEFRAME_H1 = 16385

        def __init__(self, cap):
            self.cap = cap
            self.asked = []

        def copy_rates_from_pos(self, symbol, tf, start, count):
            self.asked.append(count)
            return None if count > self.cap else np.zeros(count)

    t = Terminal(cap=20000)
    got = rb.fetch_rates(t, "EURUSD", 100000)
    check("steps down past the rejected size", got is not None and len(got), 20000)
    check("tried the requested size first", t.asked[0], 100000)

    # A terminal with nothing at all must report nothing, not a short array.
    empty = Terminal(cap=10)
    check("no usable history returns None", rb.fetch_rates(empty, "EURUSD", 100000), None)


# -------------------------------------------------------------------- stats
def test_stats_reports_absence():
    print("\nStatistics - an empty record must say so rather than imply zero")
    s = rb.stats([], 1.0)
    check("no trades is reported as a count, not a performance summary",
          s.get("trades"), 0)
    check("no win rate is invented for an empty record", "win_rate" in s, False)

    losing = [{"net": -1.0, "gross": -1.0, "bars": 3, "reason": "sl"} for _ in range(10)]
    s2 = rb.stats(losing, 1.0)
    check("a wholly losing record has win rate 0", s2["win_rate"], 0.0)
    check("profit factor is 0 when there are no wins", s2["profit_factor"], 0.0)
    check("a uniform record has no dispersion to test", s2["t_stat"], 0.0)
    check("significance is not claimed without dispersion", s2["significant_at_95"], False)


def main():
    print("rule_backtest / mt5_paper checks")
    test_filling_mode()
    test_indicators_match_live()
    test_entry_uses_closed_bars_only()
    test_straddled_bar_books_the_loss()
    test_spread_is_charged_once_per_trade()
    test_no_overlapping_positions()
    test_fetch_rates_steps_down()
    test_stats_reports_absence()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
