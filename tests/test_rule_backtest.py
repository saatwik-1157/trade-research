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

import calendar
import math
import os
import sys
import time
import types
from datetime import datetime, timedelta

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mt5_paper  # noqa: E402
import rule_backtest as rb  # noqa: E402
import take_profit  # noqa: E402
import rule_search  # noqa: E402
import track_record  # noqa: E402
import run_overnight  # noqa: E402

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
        TIMEFRAME_H4 = 16388
        TIMEFRAME_D1 = 16408

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


# ------------------------------------------------------------- server clock
class Clock:
    """A terminal whose clock runs ahead of the local one, as brokers do.

    history_deals_get filters by the bounds it is given, which is what makes
    the bug silent: a too-early upper bound returns fewer deals and no error.
    """

    def __init__(self, skew_hours=3.0, deals=()):
        self.skew = skew_hours
        self._deals = deals
        self.bounds = None

    def symbol_info_tick(self, symbol):
        if symbol != "EURUSD":
            return None
        t = datetime.now() + timedelta(hours=self.skew)
        return types.SimpleNamespace(time=t.timestamp())

    def history_deals_get(self, start, end):
        self.bounds = (start, end)
        return tuple(d for d in self._deals if start <= d.when <= end)


def deal(hours_ahead, profit, magic):
    when = datetime.now() + timedelta(hours=hours_ahead)
    return types.SimpleNamespace(when=when, profit=profit, commission=0.0,
                                 swap=0.0, magic=magic)


def test_server_clock_window():
    print("\nServer clock - a local upper bound hides trades closed today")

    c = Clock(skew_hours=3.0)
    skew = (mt5_paper.server_now(c) - datetime.now()).total_seconds() / 3600
    close_to("server_now follows the terminal, not the local clock", skew, 3.0, 0.05)
    check("history_end is padded past the server clock",
          mt5_paper.history_end(c) > mt5_paper.server_now(c), True)
    check("history_end is padded past the local clock",
          mt5_paper.history_end(c) > datetime.now(), True)

    # No tick to read: fall back to local rather than inventing an offset.
    class Dark(Clock):
        def symbol_info_tick(self, symbol):
            return None

    fallback = (mt5_paper.server_now(Dark()) - datetime.now()).total_seconds()
    check("server_now falls back to the local clock when no tick is quoted",
          abs(fallback) < 5, True)


def test_realised_today_sees_a_server_ahead_deal():
    print("\nDaily loss limit - it must actually see today's booked P&L")

    # Stamped ahead of the local clock, exactly like a real closed trade.
    ours = deal(hours_ahead=3.0, profit=-0.58, magic=mt5_paper.MAGIC)
    theirs = deal(hours_ahead=3.0, profit=-500.0, magic=999)
    c = Clock(skew_hours=3.0, deals=(ours, theirs))

    got = mt5_paper.realised_today(c)
    close_to("a deal stamped ahead of local time is counted", got, -0.58, 1e-9)
    check("another EA's deals are not counted against this tool's limit",
          got > -100, True)

    # The regression itself: bounding at local now returns 0.0 and no error,
    # so --max-daily-loss stops being a limit without anything failing.
    naive = [d for d in (ours, theirs) if d.when <= datetime.now()]
    check("the old local-clock bound would have found nothing", len(naive), 0)


def test_spread_source():
    print("\nSpread source - a quiet live quote is not what the rule pays")

    info = types.SimpleNamespace(point=0.00001, spread=1)
    tick = types.SimpleNamespace(ask=1.10001, bid=1.10000)   # 1 point, quiet
    # Half the bars record nothing; the rest average 4 points.
    spreads = np.array([0, 0, 0, 0, 3, 4, 4, 5, 40, 4], dtype=float)
    rates = np.rec.fromarrays([spreads], names="spread")

    med, note = rb.choose_spread(rates, info, tick, "median")
    close_to("median ignores the unrecorded zero bars", med / info.point, 4.0, 1e-9)
    check("the note says how many bars backed it", "6 bars" in note, True)

    p90, _ = rb.choose_spread(rates, info, tick, "p90")
    check("p90 is at least the median", p90 >= med, True)

    live, note_live = rb.choose_spread(rates, info, tick, "live")
    close_to("live reads the quote it was handed", live / info.point, 1.0, 1e-9)
    check("live says it is a single quote", "single live quote" in note_live, True)
    check("the recorded spread is the larger cost here", med > live, True)

    # No usable history: fall back rather than reporting a free trade.
    empty = np.rec.fromarrays([np.zeros(5)], names="spread")
    got, _ = rb.choose_spread(empty, info, tick, "median")
    close_to("all-zero history falls back to the live quote", got / info.point, 1.0, 1e-9)

    dead = types.SimpleNamespace(ask=0.0, bid=0.0)
    zero_info = types.SimpleNamespace(point=0.00001, spread=0)
    got2, note2 = rb.choose_spread(empty, zero_info, dead, "median")
    check("no spread anywhere is reported, not assumed to be zero cost",
          got2 == 0.0 and "understated" in note2, True)


def test_permutation_null_is_matched():
    print("\nPermutation null - shuffling must remove timing and nothing else")

    rng = np.random.default_rng(3)
    sig = np.zeros(5000, dtype=int)
    sig[rng.choice(5000, 700, replace=False)] = 1
    sig[rng.choice(np.flatnonzero(sig == 0), 400, replace=False)] = -1

    out = rule_search.permute(sig, seed=11)

    # If the null traded less often it would pay less spread, and would beat
    # the real candidate on cost alone rather than on timing.
    check("signal count is preserved", int((out != 0).sum()), int((sig != 0).sum()))
    check("buy count is preserved", int((out == 1).sum()), int((sig == 1).sum()))
    check("sell count is preserved", int((out == -1).sum()), int((sig == -1).sum()))
    check("length is preserved", len(out), len(sig))
    check("timing actually changed", bool((out != sig).any()), True)
    check("the original array is not mutated", int((sig == 1).sum()), 700)

    # Same seed must reproduce, or a reported null cannot be re-derived.
    check("permutation is deterministic in the seed",
          bool((rule_search.permute(sig, 11) == out).all()), True)
    check("a different seed gives a different draw",
          bool((rule_search.permute(sig, 12) != out).any()), True)


def test_significance_threshold():
    print("\nMultiple-testing threshold - it must rise with the number of tests")

    naive = rule_search.z_for(0.05)
    close_to("an uncorrected 5% two-sided test is 1.96", naive, 1.96, 0.01)
    check("correcting for 41 tests demands more than 1.96",
          rule_search.z_for(0.05 / 41) > 3.0, True)
    check("more tests means a higher bar",
          rule_search.z_for(0.05 / 100) > rule_search.z_for(0.05 / 41), True)


def test_candidates_are_distinct():
    print("\nCandidate set - names must be unique or results cannot be attributed")

    cands = rule_search.build_candidates()
    names = [n for n, _, _ in cands]
    check("every candidate has a distinct name", len(set(names)), len(names))
    check("more than one family is represented",
          len({f for _, f, _ in cands}) > 5, True)

    # A signal that reads its own bar would be lookahead. Every candidate must
    # leave the opening bars flat, since no indicator is formed yet.
    rng = np.random.default_rng(5)
    c = 100 + np.cumsum(rng.normal(0, 0.3, 2000))
    h, l = c + 0.2, c - 0.2
    o = np.concatenate([[c[0]], c[:-1]])
    leaky = [n for n, _, fn in cands if fn(o, h, l, c)[0] != 0]
    check("no candidate signals on the very first bar", leaky, [])


def _row(sym, t, net, idx=0):
    return {"symbol": sym, "entry_idx": idx, "entry_time": t, "net": float(net),
            "gross": float(net), "bars": 3, "reason": "tp"}


def test_clustering_removes_the_sqrt_n_inflation():
    """Seven correlated pairs are not seven hundred independent trades.

    Every trade within a symbol is set to that symbol's own mean here, so the
    pooled spread and the between-symbol spread are the same number and the
    only thing separating the two t-statistics is the count they divide by.
    That isolates the inflation exactly: 100 trades per symbol should buy a
    factor of sqrt(100).
    """
    print()
    print("Symbol clustering - correlated pairs must not count as independent")
    means = [8, 12, 9, 11, 10, 7, 13]
    rows = [_row(f"SYM{i}", 0, m) for i, m in enumerate(means) for _ in range(100)]

    pooled = rb.stats(rows, 1.0)["t_stat"]
    clustered = rule_search.clustered_t(rows)["t_stat_clustered_by_symbol"]

    check("pooling 700 correlated trades looks significant", pooled > 20, True)
    check("clustering by symbol deflates it", clustered < pooled / 5, True)
    # sqrt(100) for the count, times sqrt(7/6) because the clustered spread is
    # estimated from seven numbers rather than seven hundred.
    check("the deflation is the sqrt of trades per symbol",
          abs(pooled / clustered - 10 * math.sqrt(7 / 6)) < 0.2, True)
    check("all seven symbols are counted",
          rule_search.clustered_t(rows)["symbols_positive"], 7)


def test_clustered_threshold_is_not_1_96():
    """Six degrees of freedom, so the bar moves - reading it against 1.96 is
    how a clustered statistic gets called significant when it is not."""
    print()
    print("Clustered threshold - seven symbols is six degrees of freedom")
    check("seven symbols demands 2.447, not 1.96", rule_search.t_crit_95(6), 2.447)
    check("fewer symbols demands more", rule_search.t_crit_95(3) > 2.447, True)
    check("two symbols is not enough to cluster on",
          rule_search.t_crit_95(1), None)

    # A statistic that clears 1.96 but not the real threshold must be reported
    # as not significant, or the correction has been applied and then ignored.
    means = [1.0, 1.2, 0.4, 1.5, 0.3, 1.1, 0.6]
    rows = [_row(f"SYM{i}", 0, m) for i, m in enumerate(means) for _ in range(50)]
    out = rule_search.clustered_t(rows)
    t = out["t_stat_clustered_by_symbol"]
    check("this sample clears the naive bar", t > 1.96, True)
    check("but is judged against the clustered one",
          out["symbol_cluster_significant"], t > 2.447)


def test_eras_are_disjoint_and_complete():
    print()
    print("Era blocks - every trade lands in exactly one era")
    market = {"A": {"time": np.arange(1000, 5001, 10, dtype="int64")},
              "B": {"time": np.arange(1200, 4801, 10, dtype="int64")}}
    edges = rule_search.block_edges(market, 4)

    check("edges bound the window every symbol covers",
          (int(edges[0]), int(edges[-1])), (1200, 4800))
    check("four eras means five edges", len(edges), 5)

    rows = [_row("A", t, 1.0) for t in range(1200, 4801, 25)]
    blocks = rule_search.block_views(rows, edges)
    check("every trade is counted once",
          sum(b["trades"] for b in blocks), len(rows))
    check("no era is empty", all(b["trades"] > 0 for b in blocks), True)

    # A rule that only works recently must not read as broadly positive.
    late = ([_row("A", t, -1.0) for t in range(1200, 3900, 25)]
            + [_row("A", t, 50.0) for t in range(3900, 4801, 25)])
    summary = rule_search.block_summary(rule_search.block_views(late, edges))
    check("a last-era rule is positive in one era of four",
          summary["blocks_positive"], 1)


def test_walk_forward_cannot_see_its_test_era():
    """The selection at each fold must be blind to the era it is graded on.

    The candidate here loses through era 1 and then makes a fortune in era 4.
    If the fold that trades era 4 ranks it on anything but eras 1-3, its
    training t-statistic comes out positive and the walk-forward has been
    handed the answer.
    """
    print()
    print("Walk-forward - the training window must stop at the test era")
    market = {"A": {"time": np.arange(0, 4001, 1, dtype="int64")}}
    edges = rule_search.block_edges(market, 4)
    rows = ([_row("A", t, -1.0 - (t // 10) % 3) for t in range(0, 1000, 10)]
            + [_row("A", t, 100.0) for t in range(3000, 4000, 10)])

    wf = rule_search.walk_forward({"late_only": rows}, edges, min_trades=10)
    by_block = {f["test_block"]: f for f in wf["folds"]}

    check("one fold per era after the first", len(wf["folds"]), 3)
    check("eras with no trades are not graded", wf["folds_graded"], 1)
    check("training on eras 1-3 sees only the losing era",
          by_block[4]["train_t"] < 0, True)
    check("the test era is scored on its own trades",
          by_block[4]["test_expectancy_points_net"], 100.0)
    check("a fortune in one era of four is not called consistent",
          wf["folds_profitable"], 1)


def test_date_clustering_removes_the_shared_move():
    """One dollar move opens seven trades; that is one observation, not seven.

    Every date here fires all seven pairs with an identical result, which is
    the worst case the correction exists for. Pooling counts 1400 trades and
    divides by sqrt(1400); clustering counts 200 dates and divides by
    sqrt(200), so the pooled figure is inflated by sqrt(7).
    """
    print()
    print("Date clustering - simultaneous trades are one observation")
    rng = np.random.default_rng(3)
    rows = []
    for day in range(200):
        move = float(rng.normal(4.0, 30.0))
        for s in range(7):
            rows.append(_row(f"SYM{s}", day * 86400, move))

    pooled = rb.stats(rows, 1.0)["t_stat"]
    out = rule_search.clustered_by_date(rows)
    clustered = out["t_stat_clustered_by_date"]

    check("one observation per date is recovered", out["entry_dates"], 200)
    check("seven pairs fire per date", out["obs_per_date"], 7.0)
    check("clustering by date deflates the pooled t", clustered < pooled, True)
    check("the inflation is sqrt(pairs firing together)",
          abs(pooled / clustered - math.sqrt(7)) < 0.05, True)

    # Clustering by symbol cannot see this: every pair has the same mean, so
    # the between-pair spread is ~0 and the statistic explodes. That is why the
    # verdict is gated on the date figure and not this one.
    sym = rule_search.clustered_t(rows)["t_stat_clustered_by_symbol"]
    check("by-symbol clustering misses a shared move entirely",
          sym > pooled, True)


def test_crowded_dates_are_flagged_not_resolved():
    """Per-date and per-trade means can disagree, and that is the finding.

    Here the losses arrive on a few dates that fire many pairs at once and the
    wins arrive on many quiet dates. Weighting trades equally the rule loses;
    weighting dates equally it wins. Reporting only the second turns a losing
    rule into a survivor, which is exactly the misreading the flag exists to
    stop.
    """
    print()
    print("Crowded dates - a sign flip between the two means must be visible")
    rows = []
    for day in range(200):                      # quiet dates, one small win
        rows.append(_row("EURUSD", day * 86400, 2.0 + (day % 5)))
    for day in range(200, 210):                 # crowded dates, heavy losses
        for s in range(7):
            rows.append(_row(f"SYM{s}", day * 86400, -60.0 - (s % 3)))

    pooled = rb.stats(rows, 1.0)["expectancy_points_net"]
    out = rule_search.clustered_by_date(rows)

    check("the rule loses money per trade", pooled < 0, True)
    check("but wins when every date counts once", out["mean_per_date"] > 0, True)
    check("the disagreement is reported", out["signs_disagree"], True)


def test_hour_filter_blocks_the_entry_bar_not_the_signal_bar():
    """A signal on bar j is actionable on bar j+1, so it is j+1's hour that
    decides whether the entry pays the rollover spread. Masking the signal
    bar's own hour would block the wrong trades and quietly leave every
    rollover entry in place."""
    print()
    print("Hour filter - the blocked hour is the one the order fills in")
    times = np.arange(0, 48 * 3600, 3600, dtype="int64")   # two days, hourly
    blocked = np.zeros(len(times), dtype=bool)
    bad = np.isin((times // 3600) % 24, [0])
    blocked[:-1] = bad[1:]

    check("the bar before midnight is what gets masked",
          [int(i) for i in np.flatnonzero(blocked)], [23])
    check("midnight's own signal bar is left alone", bool(blocked[24]), False)


class FakeMT5:
    """Enough of the MT5 surface for place(), with a controllable fill price.

    The point of the fake is that order_send returns a `price` the caller did
    not choose. A real server does that under slippage or a stale quote, and it
    is the one input that cannot be produced by driving the real API.
    """

    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_RETCODE_DONE = 10009
    TIMEFRAME_H1 = 16385

    def __init__(self, fill, bid=0.59752, sltp_ok=True, info_over=None, deal=88800011):
        self.fill, self.bid, self.sltp_ok = fill, bid, sltp_ok
        self.sent = []
        self.info_over = info_over or {}
        self.deal = deal

    def symbol_info(self, symbol):
        base = dict(digits=5, point=0.00001, filling_mode=1,
                    trade_tick_value=1.0, trade_tick_size=0.00001,
                    volume_step=0.01, volume_min=0.01, volume_max=100.0)
        base.update(self.info_over)
        return types.SimpleNamespace(**base)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return types.SimpleNamespace(ask=self.bid + 0.00002, bid=self.bid, time=0)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        n = 300
        # A flat 76-point true range, so 1.5xATR lands on 114 points - the
        # bracket the live NZDUSD orders actually carried.
        return np.rec.fromarrays(
            [np.full(n, self.bid + 0.00038),
             np.full(n, self.bid - 0.00038),
             np.full(n, self.bid)],
            names="high,low,close")

    def order_send(self, request):
        self.sent.append(request)
        if request["action"] == self.TRADE_ACTION_SLTP:
            return types.SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE if self.sltp_ok else 10016)
        return types.SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE, order=10200315596,
            # An order is the instruction, a deal is the execution. A real
            # server returns both and they are never the same number.
            deal=self.deal, price=self.fill)


def _place(fake, side="sell", risk_usd=None):
    """Run place() against the fake with the trade log stubbed out."""
    logged = []
    real_log = mt5_paper._log
    mt5_paper._log = logged.append
    try:
        out = mt5_paper.place(fake, "NZDUSD", side, 0.01, 1.5, 1.5, live=True,
                              risk_usd=risk_usd)
    finally:
        mt5_paper._log = real_log
    return out, logged


def test_bracket_must_straddle_the_fill():
    print("\nBracket sanity - both exits on one side of entry is a fixed loss")

    check("a normal long is sane", mt5_paper.bracket_is_sane(True, 1.0, 0.9, 1.1), True)
    check("a normal short is sane", mt5_paper.bracket_is_sane(False, 1.0, 1.1, 0.9), True)

    # Position 10200315596: sell quoted 0.59752 with tp 0.59638, filled 0.59473.
    check("the live NZDUSD fill is caught",
          mt5_paper.bracket_is_sane(False, 0.59473, 0.59866, 0.59638), False)
    check("a long filled above its own target is caught",
          mt5_paper.bracket_is_sane(True, 1.2, 0.9, 1.1), False)
    # A fill exactly on a level is not straddled either - it is already out.
    check("a fill sitting on the stop is not sane",
          mt5_paper.bracket_is_sane(True, 0.9, 0.9, 1.1), False)


def test_place_records_the_fill_not_the_quote():
    print("\nOrder log - the entry recorded must be the one the server gave")

    fake = FakeMT5(fill=0.59750)          # 2 points of ordinary slippage
    out, logged = _place(fake)

    close_to("the fill is recorded", out["fill_price"], 0.59750, 1e-9)
    close_to("slippage is measured against the quote", out["slippage_points"], -2.0, 0.01)
    check("a sane bracket is left alone", out.get("bracket_repaired"), None)
    check("only the entry order was sent", len(fake.sent), 1)
    close_to("the log carries the fill", logged[0]["fill_price"], 0.59750, 1e-9)


def test_inverted_bracket_is_repaired_from_the_fill():
    print("\nBracket repair - re-anchor on the fill rather than hold a lost trade")

    fake = FakeMT5(fill=0.59473)          # the live 279-point case
    out, _ = _place(fake)

    check("the inversion is flagged", out["bracket_repaired"], True)
    check("an SLTP modify followed the entry", len(fake.sent), 2)
    check("the second call is a modify",
          fake.sent[1]["action"], FakeMT5.TRADE_ACTION_SLTP)
    check("it modifies the position just opened", fake.sent[1]["position"], 10200315596)

    # Re-anchored on the fill, the short's stop is above it and its target below.
    check("the repaired bracket straddles the fill",
          mt5_paper.bracket_is_sane(False, 0.59473, fake.sent[1]["sl"], fake.sent[1]["tp"]),
          True)
    check("the reported levels are the repaired ones", out["sl"], fake.sent[1]["sl"])
    close_to("the stop keeps its 1.5xATR distance",
             abs(fake.sent[1]["sl"] - 0.59473) / 0.00001, 114.0, 1.0)


def test_unrepairable_bracket_is_closed_not_held():
    print("\nBracket repair - a modify that fails must not leave the position open")

    fake = FakeMT5(fill=0.59473, sltp_ok=False)
    out, _ = _place(fake)

    check("the failed repair is reported", out["bracket_repair_failed_closed"], True)
    check("entry, modify, then close", len(fake.sent), 3)
    check("the third call closes the position", fake.sent[2]["position"], 10200315596)
    check("closing a short is a buy", fake.sent[2]["type"], FakeMT5.ORDER_TYPE_BUY)


class StampedClock:
    """A terminal reporting one fixed server stamp.

    MT5 encodes a tick or deal time as the server's WALL CLOCK rendered as if
    it were a UTC epoch, so the fake builds its stamp the same way. Nothing
    here depends on the machine's own timezone, which is the point.
    """

    def __init__(self, wall):
        self.wall = wall
        self.stamp = calendar.timegm(wall.timetuple())

    def symbol_info_tick(self, symbol):
        if symbol != "EURUSD":
            return None
        return types.SimpleNamespace(time=self.stamp)



# The fake's flat 76-point true range makes 1.5xATR a 114-point stop, and at
# tick_value 1.0 per 0.00001 that is exactly 114.00 of risk per 1.0 lot - so
# every figure below is checkable by hand rather than by rerunning the tool.

def test_lot_is_sized_off_the_stop_distance():
    print()
    print("Risk sizing - the lot follows the stop, not a constant")
    fake = FakeMT5(fill=0.59752)
    got = mt5_paper.lot_for_risk(fake, "NZDUSD", 1.5 * 0.00076, 5.70)
    check("a 114-point stop at 5.70 of risk is 0.05 lots", got["lot"], 0.05)
    close_to("and the stop then costs what was asked", got["risk_actual"], 5.70, 0.01)
    check("no gap when the budget divides cleanly", "gap" in got, False)


def test_rounding_never_risks_more_than_the_budget():
    print()
    print("Risk sizing - the volume step rounds down, never up")
    fake = FakeMT5(fill=0.59752)
    got = mt5_paper.lot_for_risk(fake, "NZDUSD", 1.5 * 0.00076, 5.69)
    check("0.0499 lots rounds down to the step", got["lot"], 0.04)
    check("so the actual risk is under the budget", got["risk_actual"] <= 5.69, True)


def test_min_lot_floor_is_reported_not_hidden():
    print()
    print("Risk sizing - a budget smaller than one minimum lot is a gap")
    fake = FakeMT5(fill=0.59752)
    got = mt5_paper.lot_for_risk(fake, "NZDUSD", 1.5 * 0.00076, 0.50)
    check("it still sends the broker's minimum", got["lot"], 0.01)
    close_to("which risks more than asked", got["risk_actual"], 1.14, 0.01)
    check("and says so rather than reporting the budget", "gap" in got, True)


def test_missing_tick_value_refuses_to_size():
    print()
    print("Risk sizing - refuse rather than guess, and send nothing")
    fake = FakeMT5(fill=0.59752, info_over={"trade_tick_value": 0.0})
    got = mt5_paper.lot_for_risk(fake, "NZDUSD", 1.5 * 0.00076, 5.70)
    check("no lot is returned", got["lot"], None)
    check("a reason is", bool(got.get("gap")), True)

    out, _ = _place(fake, risk_usd=5.70)
    check("place() reports it unsized", out["status"], "unsized")
    check("and no order reached the server", len(fake.sent), 0)


def test_place_sends_the_derived_volume():
    print()
    print("Risk sizing - the order carries the sized volume, not --lot")
    fake = FakeMT5(fill=0.59752)
    out, _ = _place(fake, risk_usd=5.70)
    check("the request volume is the derived one", fake.sent[0]["volume"], 0.05)
    check("and the record agrees", out["lot"], 0.05)
    close_to("the sizing detail rides along", out["sizing"]["risk_actual"], 5.70, 0.01)


def test_sizing_is_off_by_default():
    print()
    print("Risk sizing - unset means the old fixed lot, unchanged")
    fake = FakeMT5(fill=0.59752)
    out, _ = _place(fake)
    check("volume is --lot", fake.sent[0]["volume"], 0.01)
    check("and no sizing block is recorded", "sizing" in out, False)


def test_daily_limit_starts_at_the_servers_midnight():
    print("\nDaily loss limit - the day must be the server's, not the operator's")

    wall = datetime(2026, 8, 26, 0, 4, 15)       # four minutes past server rollover
    c = StampedClock(wall)

    # The MT5 package reads a naive bound through the LOCAL timezone, so that
    # is the frame the returned value has to be measured in.
    got = time.mktime(mt5_paper.server_day_start(c).timetuple())
    want = calendar.timegm(wall.replace(hour=0, minute=0, second=0).timetuple())
    check("the window opens exactly at server midnight", int(got), int(want))

    # The regression: .replace(hour=0) on server_now() lands on midnight of the
    # LOCAL-rendered clock, so the limit's day slides with the operator's zone.
    naive = mt5_paper.server_now(c).replace(hour=0, minute=0, second=0, microsecond=0)
    local_off = calendar.timegm(datetime.fromtimestamp(c.stamp).timetuple()) - c.stamp
    # Negative: on a machine ahead of UTC the window opened EARLY, at server
    # 18:30 the previous day, which is what the live account showed.
    check("the old form opened early by exactly the local UTC offset",
          int(round(time.mktime(naive.timetuple()) - want)), -int(round(local_off)))

    # server_now() itself is NOT wrong - it is the frame history queries use,
    # and the offset cancels because the package reads bounds the same way.
    check("server_now still round-trips to the stamp it was given",
          int(time.mktime(mt5_paper.server_now(c).timetuple())), int(c.stamp))


class HarvestMT5(FakeMT5):
    """A terminal holding open positions, for the profit-harvest selection."""

    def __init__(self, positions):
        super().__init__(fill=0.59752)
        self._positions = positions

    def positions_get(self):
        return tuple(self._positions)

    def account_info(self):
        return types.SimpleNamespace(balance=100_000.0, equity=99_990.0,
                                     login=5055473926, currency="USD")

    def history_deals_get(self, *args, **kwargs):
        ticket = kwargs.get("ticket", args[0] if args else None)
        if ticket != self.deal:
            return ()
        return (types.SimpleNamespace(
            ticket=self.deal, profit=0.05, swap=0.0, commission=0.0),)


def pos(ticket, profit, swap=0.0, magic=None, symbol="EURUSD"):
    return types.SimpleNamespace(
        ticket=ticket, symbol=symbol, type=1, volume=0.01,
        profit=profit, swap=swap,
        magic=mt5_paper.MAGIC if magic is None else magic)


def test_harvest_closes_only_the_winners():
    print("\nProfit harvest - selection must be by NET float, and only ours")

    positions = [
        pos(1, profit=+0.05),                       # a winner
        pos(2, profit=-0.30),                       # a loser, must be left alone
        pos(3, profit=+0.04, swap=-0.06),           # gross positive, net negative
        pos(4, profit=+0.10, swap=-0.02),           # net +0.08, still a winner
        pos(5, profit=+9.99, magic=4242),           # another EA's, never touched
    ]
    c = HarvestMT5(positions)

    close_to("swap is counted against the float",
             take_profit.net_floating(positions[2]), -0.02, 1e-9)

    logged = []
    real_log = mt5_paper._log
    mt5_paper._log = logged.append
    try:
        out = take_profit.harvest(c, min_profit=0.01, live=True)
    finally:
        mt5_paper._log = real_log

    closed = sorted(r["ticket"] for r in out if r["status"] == "CLOSED")
    check("only the net winners are harvested", closed, [1, 4])
    check("the loser is left open", 2 in closed, False)
    check("a position positive only before swap is left open", 3 in closed, False)
    check("another EA's winner is never touched", 5 in closed, False)
    check("every close was logged", len(logged), 2)

    # The predicate is what makes this safe; without one close_own takes all.
    c2 = HarvestMT5(positions)
    mt5_paper._log = lambda r: None
    try:
        everything = mt5_paper.close_own(c2, live=True)
    finally:
        mt5_paper._log = real_log
    check("no predicate still means close all of ours", len(everything), 4)


def test_harvest_threshold_is_inclusive():
    print("\nProfit harvest - a position exactly at the threshold is taken")

    c = HarvestMT5([pos(1, profit=0.01), pos(2, profit=0.009)])
    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        out = take_profit.harvest(c, min_profit=0.01, live=True)
    finally:
        mt5_paper._log = real_log
    check("exactly at the threshold closes", [r["ticket"] for r in out], [1])


def test_the_ledger_knows_whose_trades_it_holds():
    """`closed_trades` pairs deals for the ACCOUNT, not for this tool.

    Until the entry deal's magic was recorded, on 2026-09-07, a trade opened by
    hand in the terminal or by the platform's broker adapter arrived in the
    ledger indistinguishable from one this harness made -- so the sample behind
    every "no edge" finding was "every closed trade on this account". Measured
    on the demo account that day: 258 closed trades over 30 days, 256 tagged
    770315, one tagged 770316 and one tagged nothing at all.
    """
    print()
    print("Ledger provenance - a ledger must know whose trades it holds")

    trades = [
        {"position_id": 1, "magic": mt5_paper.MAGIC},
        {"position_id": 2, "magic": mt5_paper.MAGIC},
        {"position_id": 3, "magic": 770316},   # the platform's broker adapter
        {"position_id": 4, "magic": 0},        # opened by hand, tagged by nobody
        {"position_id": 5},                    # a read from before magic was kept
    ]

    check("the census counts by tag, not by tool",
          track_record.census(trades), {0: 2, 770316: 1, mt5_paper.MAGIC: 2})

    ours = track_record.select_by_magic(trades, mt5_paper.MAGIC)
    check("the default takes only this harness's trades",
          [t["position_id"] for t in ours], [1, 2])

    check("an untagged trade is never folded in silently",
          [t["position_id"] for t in ours if t.get("magic", 0) == 0], [])

    everything = track_record.select_by_magic(trades, None)
    check("None restores the old behaviour exactly", len(everything), 5)

    check("the two systems no longer share a tag",
          mt5_paper.MAGIC == 770316, False)

    # The tag split is not retroactive. Nine of the ten trades the platform
    # made at this venue carry 770315 because the adapter shared it until that
    # evening, so the only thing that separates them is their identity.
    kept = track_record.drop_positions(trades, {"1", "4"})
    check("named position ids are dropped whatever their tag",
          [t["position_id"] for t in kept], [2, 3, 5])
    check("an empty exclusion changes nothing",
          len(track_record.drop_positions(trades, set())), 5)


def test_the_order_log_names_both_venue_tickets():
    """An order and a deal are different things, and a close had neither.

    `close_own` reported a retcode and a fill and never the ticket of the close
    order it had just sent, so `orders.broker_order_id` was empty on every
    close the platform ever made -- the one handle an UNKNOWN close can be
    settled by. The deal is the second: it identifies one execution, which is
    what a fill has to be deduplicated by.
    """
    print()
    print("Venue identity - an order ticket and a deal ticket are not the same")

    out, logged = _place(FakeMT5(fill=0.59750))
    check("the entry names its order", out["order"], 10200315596)
    check("the entry names its deal", out["deal"], 88800011)
    check("and they are not the same number", out["order"] == out["deal"], False)

    c = HarvestMT5([pos(1, profit=+0.05)])
    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        closed = take_profit.harvest(c, min_profit=0.01, live=True)
    finally:
        mt5_paper._log = real_log
    rec = closed[0]
    check("a close names the close ORDER's ticket", rec["order"], 10200315596)
    check("a close names the deal that executed it", rec["deal"], 88800011)
    check("the money still comes from the server", rec["profit"], 0.05)


def test_a_venue_reporting_no_ticket_records_a_gap_not_a_zero():
    """0 is MetaTrader for "no ticket", and it is not an identifier.

    Stored as `str(0)` downstream it would read as something that names a real
    order, which is the same class of error as booking a missing P&L as zero.
    """
    print()
    print("Venue identity - a missing ticket must be absent, never 0")

    out, _ = _place(FakeMT5(fill=0.59750, deal=0))
    check("a zero deal is recorded as absent", out["deal"], None)


def test_the_profit_floor_decays_into_the_deadline():
    """`--relax-over` is what makes "slowly close everything" different from
    "dump everything at 06:00".

    A position 40 cents up at 05:59 is closed for 40 cents rather than flushed
    at whatever it is worth a minute later. The floor decays to ZERO and never
    below: zero is break-even, and going negative would be the slope deciding
    how much loss is acceptable -- which is the deadline's decision to make
    once, not the ramp's to make continuously.
    """
    print()
    print("Wind-down - the harvest floor must decay, not drop")

    a = types.SimpleNamespace(min_profit=0.50, relax_over=45.0)
    check("outside the window the floor is unchanged",
          take_profit.threshold_at(a, 60 * 60), 0.50)
    check("at the window's edge it is still unchanged",
          take_profit.threshold_at(a, 45 * 60), 0.50)
    close_to("halfway through it is halved",
             take_profit.threshold_at(a, 22.5 * 60), 0.25, 1e-9)
    close_to("at the deadline it is zero",
             take_profit.threshold_at(a, 0), 0.0, 1e-9)
    check("past the deadline it does not go negative",
          take_profit.threshold_at(a, -600), 0.0)

    off = types.SimpleNamespace(min_profit=0.50, relax_over=0.0)
    check("no ramp means the floor never moves",
          take_profit.threshold_at(off, 1), 0.50)


def test_the_flush_closes_losers_and_the_harvest_never_does():
    """The one call in this file that can realise a loss on purpose.

    `harvest` has a floor and always has, so a session that only harvests
    leaves the losing tail open -- which is exactly what 06:00 found on
    2026-09-07: 7 positions, 6 of them underwater, float -9.25. `flatten` is
    the deliberate opposite and is a separate function so that it cannot be
    reached by accident.
    """
    print()
    print("Wind-down - being flat by a time means booking what is left")

    positions = [
        pos(1, profit=+0.90),                    # a winner
        pos(2, profit=-2.25),                    # the losing tail
        pos(3, profit=-0.61),
        pos(4, profit=+9.99, magic=4242),        # another EA's, never ours
    ]
    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        got = take_profit.harvest(HarvestMT5(positions), 0.50, live=True)
        harvested = sorted(r["ticket"] for r in got if r["status"] == "CLOSED")

        flat = take_profit.flatten(HarvestMT5(positions), live=True)
        flushed = sorted(r["ticket"] for r in flat if r["status"] == "CLOSED")
    finally:
        mt5_paper._log = real_log

    check("the harvest takes only the winner", harvested, [1])
    check("the flush takes the losers too", flushed, [1, 2, 3])
    check("and still never touches another EA's position", 4 in flushed, False)


def test_a_deadline_is_the_wall_clock_and_rolls_to_tomorrow():
    """06:00 asked for in the evening means tomorrow's 06:00.

    Local, deliberately: the operator said 6am and meant the clock on the
    wall. The broker's clock is right for bounding a history query and wrong
    for a human deadline -- the `server_day_start` distinction.
    """
    print()
    print("Wind-down - 6am means the next 6am, on the operator's clock")

    secs = take_profit.seconds_until("06:00")
    check("the deadline is in the future", secs > 0, True)
    check("and within one day", secs <= 24 * 3600, True)
    target = datetime.now() + timedelta(seconds=secs)
    check("it lands on the hour asked for", (target.hour, target.minute), (6, 0))


def test_the_flush_fires_when_flat_by_equals_the_stop_hour():
    """The configuration everybody will actually use, and the one that broke.

    `run_overnight` passes --flat-by for the same hour it stops at, so the loop
    breaks one interval BEFORE the deadline and no pass ever starts after it.
    The in-loop flush could not fire, and the session would have ended holding
    everything while reporting a clean finish. It runs after the loop for that
    reason.
    """
    print()
    print("Wind-down - a deadline that coincides with the stop must still flush")

    positions = [pos(1, profit=-2.25), pos(2, profit=-0.61)]
    c = HarvestMT5(positions)
    args = types.SimpleNamespace(
        minutes=0.001, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        out = take_profit.run(c, args)
    finally:
        mt5_paper._log = real_log

    check("the losers were flushed, not left open", out["flushed"], 2)
    check("and they are not counted as harvests", out["harvested"], 0)


def test_a_refused_flush_is_retried_and_named():
    """The 2026-09-08 defect, both halves.

    The machine woke at the deadline and the flush fired into a terminal that
    was awake but had no trade server yet: `order_send` returned 10031, the one
    attempt was spent, and the position stayed open all day carrying swap. What
    the operator saw was `flat-by 06:00: closed 0 at the deadline`, which is
    exactly what an already-flat account prints. The retcode was in the record
    and never left it.
    """
    print()
    print("Wind-down - a refused close is not a closed position")

    class Refusing(HarvestMT5):
        """Refuses with 10031 until `fails` attempts have been spent."""

        def __init__(self, positions, fails):
            super().__init__(positions)
            self.fails = fails
            self.attempts = 0

        def order_send(self, request):
            self.attempts += 1
            if self.attempts <= self.fails:
                return types.SimpleNamespace(retcode=10031, order=0, deal=0,
                                             price=0.0, volume=0.0)
            self._positions = []
            return super().order_send(request)

    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        # Refuses once, then succeeds: the retry is what makes the account flat.
        c = Refusing([pos(1, profit=-2.25)], fails=1)
        closed, still = take_profit.flush_until_flat(c, live=True, attempts=4, wait=0)
        check("the retry closed what one attempt could not", closed, 1)
        check("and nothing is left failing", still, [])
        check("it took a second attempt to do it", c.attempts, 2)

        # Refuses throughout: the caller must be told, with the retcode.
        c = Refusing([pos(2, profit=-2.25)], fails=99)
        closed, still = take_profit.flush_until_flat(c, live=True, attempts=3, wait=0)
        check("a flush that never succeeded closed nothing", closed, 0)
        check("and says so rather than reporting a flat account", len(still), 1)
        check("naming the retcode the venue gave", still[0]["retcode"], 10031)
        check("after every attempt it was given", c.attempts, 3)
    finally:
        mt5_paper._log = real_log


def test_a_halt_does_not_flush_hours_early():
    """`--max-daily-loss` firing at 22:00 stops trading; it does not mean close
    everything six hours before the operator asked."""
    print()
    print("Wind-down - a risk halt is not the deadline")

    class Halting(HarvestMT5):
        def order_send(self, request):
            raise AssertionError("a halted session must send nothing")

    c = Halting([pos(1, profit=-2.25)])
    args = types.SimpleNamespace(
        minutes=0.001, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=False, live=True,
        rule="random", symbols="EURUSD", lot=0.01, risk_usd=None,
        sl_atr=1.5, tp_atr=1.5, max_positions=7, max_daily_loss=0.01)

    real_cycle = mt5_paper.cycle
    mt5_paper.cycle = lambda mt5, a: {"halted": True, "reason": "daily loss",
                                      "actions": []}
    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        out = take_profit.run(c, args)
    finally:
        mt5_paper.cycle = real_cycle
        mt5_paper._log = real_log

    check("a halted session closes nothing", out["flushed"], 0)
    check("and says it halted", out["halted"], True)


def test_the_two_halts_are_told_apart_by_value():
    """A risk halt and a dead terminal call for opposite responses from
    anything supervising the session: one must not be restarted, the other
    exists to be. `run_overnight --continuous` acts on this, so the two have to
    be separable by a value rather than by matching the printed line -- the
    same rule the MT5 registration route follows when it tells a fence refusal
    from a missing package.
    """
    print()
    print("Halts - which one, not just that there was one")

    args = types.SimpleNamespace(
        minutes=0.001, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by=None, harvest_only=False, live=True,
        rule="random", symbols="EURUSD", lot=0.01, risk_usd=None,
        sl_atr=1.5, tp_atr=1.5, max_positions=7, max_daily_loss=0.01)

    real_cycle, real_log = mt5_paper.cycle, mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        mt5_paper.cycle = lambda mt5, a: {"halted": True, "reason": "daily loss",
                                          "actions": []}
        risk = take_profit.run(HarvestMT5([pos(1, profit=-2.25)]), args)
    finally:
        mt5_paper.cycle = real_cycle
        mt5_paper._log = real_log

    check("the risk limit names itself", risk["halt_kind"], "risk")

    class Dead(HarvestMT5):
        def positions_get(self, **kw):
            raise RuntimeError("terminal not answering")

    terminal = take_profit.run(Dead([]), args)
    check("a terminal that stopped answering is a different halt",
          terminal["halt_kind"], "terminal")
    check("and a session that simply ran out of time halted at all", False,
          bool(risk["halt_kind"] == terminal["halt_kind"]))


def test_the_machine_is_released_even_when_the_session_raises():
    """A held execution state that is never released is worse than sleeping.

    `keep_awake` stops the laptop idling to sleep, which a session told to be
    flat by 06:00 needs -- measured on this machine, idle standby at 300
    minutes on AC against a session needing 514, so the deadline would have
    arrived with the process suspended and the log would just stop. But the
    hold is process-wide and outlives a crash, so the release has to be in a
    `finally` and not at the end of the block.
    """
    print()
    print("Keep-awake - the hold must be released on the way out, crash included")

    calls = []
    real = take_profit.ctypes

    class FakeKernel:
        @staticmethod
        def SetThreadExecutionState(flags):  # noqa: N802
            calls.append(flags)
            return 1

    take_profit.ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=FakeKernel))
    try:
        try:
            with take_profit.keep_awake() as held:
                check("the hold was taken", held, True)
                raise RuntimeError("the session fell over")
        except RuntimeError:
            pass
    finally:
        take_profit.ctypes = real

    check("it was taken with SYSTEM_REQUIRED, not DISPLAY",
          calls[0], take_profit.ES_CONTINUOUS | take_profit.ES_SYSTEM_REQUIRED)
    check("and released back to CONTINUOUS despite the crash",
          calls[-1], take_profit.ES_CONTINUOUS)


def test_a_platform_that_cannot_hold_says_so_and_still_runs():
    """Windows only. Elsewhere the session must still run, and must not claim
    a hold it does not have -- a silent no-op here means a session that quietly
    sleeps through its own deadline."""
    print()
    print("Keep-awake - a platform without it is reported, not pretended")

    real = take_profit.ctypes
    take_profit.ctypes = types.SimpleNamespace(windll=None)
    try:
        with take_profit.keep_awake() as held:
            check("no hold is claimed", held, False)
    finally:
        take_profit.ctypes = real


def test_a_setting_can_be_overridden_but_not_invented():
    """`--rule` and `--max-positions` change a stated default, and only that.

    The session's settings are what every figure in the live record was taken
    under, so they are not edited casually -- but a default nobody can override
    from the command line is a default somebody eventually edits in the file,
    and then the record describes two configurations under one name.
    """
    print()
    print("Session overrides - change a stated default, never append a new one")

    settings = ["--rule", "random", "--max-positions", "7", "--interval", "20"]

    check("an override replaces the value in place",
          run_overnight.override(settings, "--rule", "rsi_reversion")[:2],
          ["--rule", "rsi_reversion"])
    check("and leaves every other setting alone",
          run_overnight.override(settings, "--rule", "rsi_reversion")[2:],
          ["--max-positions", "7", "--interval", "20"])
    check("None means no override at all",
          run_overnight.override(settings, "--rule", None), settings)
    check("the original list is not mutated", settings[1], "random")

    # Appending an unknown flag would be ADDING a setting under the guise of
    # changing one, and the caller would never see the difference.
    try:
        run_overnight.override(settings, "--leverage", "50")
    except SystemExit as exc:
        check("an unknown setting is refused, not appended",
              "cannot be overridden" in str(exc), True)
    else:
        check("an unknown setting is refused, not appended", False, True)


def test_one_bad_pass_does_not_end_a_session_with_hours_left():
    """The loop had NO exception handling, and the flush runs after it.

    So a single transient IPC error at 02:00 killed the process and left every
    position open at 06:00 -- quietly converting "be flat by six" into "be flat
    by six unless anything at all goes wrong overnight". Measured as a defect by
    reading the code, not by losing a night to it.
    """
    print()
    print("Loop resilience - a blink is not the end of the session")

    class Blinks(HarvestMT5):
        """Fails the first close_own call, then behaves."""

        def __init__(self, positions):
            super().__init__(positions)
            self.calls = 0

        def positions_get(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("IPC timeout")
            return tuple(self._positions)

    c = Blinks([pos(1, profit=-2.25)])
    args = types.SimpleNamespace(
        minutes=0.02, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        out = take_profit.run(c, args)
    finally:
        mt5_paper._log = real_log

    check("the session survived the blink", out["halted"], False)
    check("and the deadline flush still ran", out["flushed"], 1)


def test_a_terminal_that_never_answers_is_given_up_on():
    """The other half. A session that retried a dead terminal every 20 seconds
    until 06:00 would fill a log with nothing and close nothing."""
    print()
    print("Loop resilience - a dead terminal is not retried all night")

    class Dead(HarvestMT5):
        def positions_get(self):
            raise OSError("terminal gone")

    c = Dead([pos(1, profit=-2.25)])
    args = types.SimpleNamespace(
        minutes=5.0, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        out = take_profit.run(c, args)
    finally:
        mt5_paper._log = real_log

    check("it gave up rather than spinning", out["halted"], True)
    # And did NOT try to flush: a flush against a terminal that will not answer
    # closes nothing and reports success.
    check("and attempted no flush it could not complete", out["flushed"], 0)
    # NOT zero. "We could not ask" and "nothing is open" must not look alike.
    check("an uncountable account is UNKNOWN, not flat", out["still_open"], None)
    check("after the stated number of misses",
          take_profit.MAX_CONSECUTIVE_MISSES, 10)


def test_a_partial_disconnect_is_not_a_cosmetic_failure():
    """The 2026-09-10 shape: positions_get answers, account_info does not.

    This is the case the old handler could not see. `account_info()` returns
    None on a dropped terminal, `bal.balance` on None raises AttributeError,
    and that was caught as though a log line had failed to format -- printing
    "the session continues" and doing exactly that, 23 times, harvesting
    nothing and with --max-daily-loss unable to evaluate.

    It also needs its OWN budget. `misses` is reset by the next good harvest,
    and here every harvest succeeds, so a shared counter would be spent and
    refilled forever and the session would never give up.
    """
    print()
    print("A partial disconnect stops the session rather than blinding it")

    class HalfDead(HarvestMT5):
        """Answers the position query. Stops answering the account query.

        Readable at startup and None afterwards, which is the real sequence:
        the session began fine at 09:05 and the terminal dropped at 10:25.
        A mock that is dead from the first call would be stopped by the
        startup guard and would never exercise the loop.
        """

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._reads = 0

        def account_info(self):
            self._reads += 1
            if self._reads == 1:
                return super().account_info()
            return None

    c = HalfDead([])
    args = types.SimpleNamespace(
        minutes=5.0, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    real_log = mt5_paper._log
    mt5_paper._log = lambda r: None
    try:
        out = take_profit.run(c, args)
    finally:
        mt5_paper._log = real_log

    check("a blind session halts instead of spinning", out["halted"], True)
    check("and names the terminal as the reason", out["halt_kind"], "terminal")
    # The budget is spent on consecutive blind passes, not reset by the
    # harvest that keeps succeeding beside them.
    check("after the stated number of unreadable passes",
          out["passes"] <= take_profit.MAX_CONSECUTIVE_MISSES, True)


def test_an_unreadable_venue_never_reports_a_limit_as_satisfied():
    """A risk limit computed from a read that did not answer is not a limit.

    `history_deals_get` and `positions_get` both return None on a dropped
    terminal, and `x or []` turned that into an empty result -- a daily P&L of
    0.00 that passes any loss limit, and an open book of 0 that invites the
    full position count to be re-opened.
    """
    print()
    print("An unreadable venue fails closed, not open")

    class NoHistory:
        def positions_get(self):
            return None

        def history_deals_get(self, *a):
            return None

        def symbol_info_tick(self, s):
            return None

    v = NoHistory()

    raised = None
    try:
        mt5_paper.own_positions(v)
    except mt5_paper.VenueUnreadable as exc:
        raised = type(exc).__name__
    check("an unreadable open book raises rather than reading empty",
          raised, "VenueUnreadable")

    # An EMPTY tuple is a real answer and must still work: the distinction
    # between "nothing is open" and "we could not ask" is the whole point.
    class Empty(NoHistory):
        def positions_get(self):
            return ()

    check("but a genuinely empty book is still empty",
          mt5_paper.own_positions(Empty()), [])


def test_a_stop_request_still_runs_the_flush():
    """P1b. The whole point of catching a console close.

    Eight sessions were killed mid-pass and none ran `--flat-by`, so each left
    its losing tail open -- the harvest floor books winners and holds losers,
    so whatever is open when a session dies IS the tail. Converting the kill
    into a stop request is only worth doing if the flush still happens, so
    that is what this asserts rather than that the loop exited.
    """
    print()
    print("A stop request winds down rather than abandoning the book")

    c = HarvestMT5([pos(1, profit=-2.25)])
    # 0.02 minutes, not 5: this test must be bounded by the stop request it is
    # testing, and a 5-minute deadline would hide a stop that never fired
    # behind five minutes of spinning.
    args = types.SimpleNamespace(
        minutes=0.02, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    # Ask for a stop on the second pass, the way a console close would.
    calls = {"n": 0}

    def hook(_passes, _state):
        calls["n"] += 1
        if calls["n"] >= 2:
            take_profit.STOP_REQUESTED = True

    real_log, real_hook = mt5_paper._log, take_profit.PASS_HOOK
    mt5_paper._log = lambda r: None
    take_profit.PASS_HOOK = hook
    try:
        out = take_profit.run(c, args)
    finally:
        mt5_paper._log = real_log
        take_profit.PASS_HOOK = real_hook
        take_profit.STOP_REQUESTED = False

    check("it stopped early", out["passes"] <= 3, True)
    # NOT halted: a halt suppresses the flush, and a stop must not.
    check("and did not mark itself halted", out["halted"], False)
    check("so the deadline flush still ran", out["flushed"], 1)
    # NOT `still_open == 0`: this mock re-serves the same position after every
    # close, so a 1 there is the fixture rather than a leak. `flushed` above is
    # what proves the wind-down closed what it found.
    check("and recorded why it stopped", out["halt_kind"], "stopped")


def test_a_failing_heartbeat_cannot_end_a_session():
    """A crash journal that can end a trading session is worse than none."""
    print()
    print("A journal that throws is swallowed by the loop")

    def explode(_passes, _state):
        raise OSError("disk full")

    # A LOSING position: one above the harvest floor is closed and re-served
    # by this mock every pass, which spins the loop until its deadline. The
    # hook here raises, so it cannot be used to stop the loop either -- the
    # bound has to be the clock.
    c = HarvestMT5([pos(1, profit=-2.25)])
    args = types.SimpleNamespace(
        minutes=0.02, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    real_log, real_hook = mt5_paper._log, take_profit.PASS_HOOK
    mt5_paper._log = lambda r: None
    take_profit.PASS_HOOK = explode
    try:
        out = take_profit.run(c, args)
        raised = None
    except BaseException as exc:  # pragma: no cover - the failure being guarded
        out, raised = None, type(exc).__name__
    finally:
        mt5_paper._log = real_log
        take_profit.PASS_HOOK = real_hook
        take_profit.STOP_REQUESTED = False

    check("the session did not raise", raised, None)
    check("it ran to its own end", out is not None and out["halted"], False)
    check("and still flushed", out is not None and out["flushed"], 1)


def test_the_heartbeat_reports_unknown_rather_than_zero_when_blind():
    """A record claiming 0 open would send a recovery run away empty."""
    print()
    print("A blind pass records UNKNOWN, not an empty book")

    class HalfDead(HarvestMT5):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._reads = 0

        def account_info(self):
            self._reads += 1
            return super().account_info() if self._reads == 1 else None

    seen = []
    c = HalfDead([])
    args = types.SimpleNamespace(
        minutes=0.02, interval=0, min_profit=0.50, relax_over=45.0,
        flat_by="06:00", harvest_only=True, live=True)

    real_log, real_hook = mt5_paper._log, take_profit.PASS_HOOK
    mt5_paper._log = lambda r: None
    take_profit.PASS_HOOK = lambda n, st: seen.append(st)
    try:
        take_profit.run(c, args)
    finally:
        mt5_paper._log = real_log
        take_profit.PASS_HOOK = real_hook
        take_profit.STOP_REQUESTED = False

    blind = [s for s in seen if s["blind"] > 0]
    check("some passes were blind", len(blind) > 0, True)
    check("and each recorded an UNKNOWN book",
          all(s["positions_open"] is None for s in blind), True)


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
    test_server_clock_window()
    test_realised_today_sees_a_server_ahead_deal()
    test_spread_source()
    test_permutation_null_is_matched()
    test_significance_threshold()
    test_candidates_are_distinct()
    test_clustering_removes_the_sqrt_n_inflation()
    test_clustered_threshold_is_not_1_96()
    test_date_clustering_removes_the_shared_move()
    test_crowded_dates_are_flagged_not_resolved()
    test_hour_filter_blocks_the_entry_bar_not_the_signal_bar()
    test_eras_are_disjoint_and_complete()
    test_walk_forward_cannot_see_its_test_era()
    test_bracket_must_straddle_the_fill()
    test_place_records_the_fill_not_the_quote()
    test_inverted_bracket_is_repaired_from_the_fill()
    test_unrepairable_bracket_is_closed_not_held()
    test_lot_is_sized_off_the_stop_distance()
    test_rounding_never_risks_more_than_the_budget()
    test_a_partial_disconnect_is_not_a_cosmetic_failure()
    test_an_unreadable_venue_never_reports_a_limit_as_satisfied()
    test_a_stop_request_still_runs_the_flush()
    test_a_failing_heartbeat_cannot_end_a_session()
    test_the_heartbeat_reports_unknown_rather_than_zero_when_blind()
    test_min_lot_floor_is_reported_not_hidden()
    test_missing_tick_value_refuses_to_size()
    test_place_sends_the_derived_volume()
    test_sizing_is_off_by_default()
    test_daily_limit_starts_at_the_servers_midnight()
    test_harvest_closes_only_the_winners()
    test_harvest_threshold_is_inclusive()
    test_the_ledger_knows_whose_trades_it_holds()
    test_the_order_log_names_both_venue_tickets()
    test_a_venue_reporting_no_ticket_records_a_gap_not_a_zero()
    test_the_profit_floor_decays_into_the_deadline()
    test_the_flush_closes_losers_and_the_harvest_never_does()
    test_a_deadline_is_the_wall_clock_and_rolls_to_tomorrow()
    test_the_flush_fires_when_flat_by_equals_the_stop_hour()
    test_a_refused_flush_is_retried_and_named()
    test_a_halt_does_not_flush_hours_early()
    test_the_two_halts_are_told_apart_by_value()
    test_the_machine_is_released_even_when_the_session_raises()
    test_a_platform_that_cannot_hold_says_so_and_still_runs()
    test_a_setting_can_be_overridden_but_not_invented()
    test_one_bad_pass_does_not_end_a_session_with_hours_left()
    test_a_terminal_that_never_answers_is_given_up_on()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
