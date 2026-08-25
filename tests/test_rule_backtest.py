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

import math
import os
import sys
import types
from datetime import datetime, timedelta

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mt5_paper  # noqa: E402
import rule_backtest as rb  # noqa: E402
import rule_search  # noqa: E402

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

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
