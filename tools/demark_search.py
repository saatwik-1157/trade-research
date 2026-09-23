#!/usr/bin/env python3
"""DeMark Sequential, and whether its countdown earns its complexity.

Twentieth search, and the last fully-specified candidate from the twelve
books. It is the only one that is a STATE MACHINE rather than a formula, and
Kaufman notes the consequence: it has no parameter to vary, so robustness
cannot be shown by sweeping a lookback the way every other search here does.
The evidence has to be cross-market and cross-era instead, which is why both
are computed.

The rules, as specified (buy side; the sell side mirrors):

  * **Setup.** Nine consecutive closes each below the close four bars earlier.
    Any close at or above that level restarts the count from zero.
  * **Intersection.** On or after the eighth setup bar, some bar's high must
    reach the low of a bar three or more earlier. Until that happens the
    setup is not armed.
  * **Countdown.** From there, count bars -- NOT necessarily consecutive --
    whose close is at or below the close two bars earlier. The signal is the
    thirteenth.
  * **Cancellation.** A close above the setup's highest high kills it; an
    opposing setup kills it; a fresh setup restarts the countdown.

**The internal control is the point of the exercise.** The setup is nine bars
of ordinary momentum, which this repository has refuted several times over.
The countdown, the intersection and the cancellation rules are the elaborate
part that is supposed to add something. So the run scores BOTH -- the setup
alone and the completed 13-count -- on the same bars. If the countdown does
not beat the setup it was built on, the machinery is decoration, and that is
the `volume_search` result restated: there the control beat the treatment, and
it was only visible because the control was in the run.

**The honest limitation, stated before the result.** A 13-count after a 9-bar
setup takes 21 bars minimum and typically 24-39, so the signal is rare. At D1
over ten years across seven majors the count is in the low hundreds, which is
thin for a t-statistic; the exact figure is printed rather than glossed, and
the verdict is read against it.

    python tools/demark_search.py
    python tools/demark_search.py --timeframe H4
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths  # noqa: F401
from rule_backtest import choose_spread, connect, fetch_rates

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "USDCHF", "NZDUSD")
SETUP_LEN = 9
COUNTDOWN_LEN = 13


def sequential(o, h, l, c, buy=True):
    """Run the machine. Returns (setup_bars, signal_bars) as index arrays.

    `setup_bars` are the bars completing a nine-bar setup; `signal_bars` are
    the bars completing a thirteen-count. Both are returned so the countdown
    can be scored against the setup it is built on.
    """
    n = len(c)
    setups, signals = [], []
    count = 0
    # state of a live countdown
    live = False
    cd = 0
    setup_high = -np.inf
    setup_low = np.inf
    armed = False
    setup_end = -1

    for t in range(4, n):
        # ---- setup -------------------------------------------------------
        cond = c[t] < c[t - 4] if buy else c[t] > c[t - 4]
        count = count + 1 if cond else 0
        if count == SETUP_LEN:
            setups.append(t)
            setup_end = t
            lo_i = t - SETUP_LEN + 1
            setup_high = float(h[lo_i:t + 1].max())
            setup_low = float(l[lo_i:t + 1].min())
            live, cd, armed = True, 0, False      # a fresh setup recycles
            count = 0

        if not live:
            continue

        # ---- cancellation ------------------------------------------------
        # A close beyond the setup's own extreme says the premise is gone.
        if buy and c[t] > setup_high:
            live = False
            continue
        if not buy and c[t] < setup_low:
            live = False
            continue

        # ---- intersection ------------------------------------------------
        # On or after the eighth setup bar, a high must reach the low of a bar
        # three or more earlier (mirrored for a sell).
        if not armed and t >= setup_end - 1 and t >= 3:
            if buy and h[t] >= l[t - 3]:
                armed = True
            elif not buy and l[t] <= h[t - 3]:
                armed = True
        if not armed:
            continue

        # ---- countdown ---------------------------------------------------
        if t >= 2:
            hit = c[t] <= c[t - 2] if buy else c[t] >= c[t - 2]
            if hit:
                cd += 1
                if cd == COUNTDOWN_LEN:
                    signals.append(t)
                    live = False
    return np.array(setups, dtype=int), np.array(signals, dtype=int)


def forward(c, idx, horizon, buy=True):
    """Log return over `horizon` bars after each index, signed so that a
    positive number means the signal was right."""
    idx = idx[idx + horizon < len(c)]
    if len(idx) == 0:
        return np.array([])
    r = np.log(c[idx + horizon] / c[idx])
    return r if buy else -r


def tstat(x):
    if len(x) < 10 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--horizons", type=int, nargs="+", default=[5, 10, 21])
    ap.add_argument("--null-rounds", type=int, default=2000)
    ap.add_argument("--out", default="reports/demark_search.json")
    args = ap.parse_args()

    mt5 = connect()
    data, costs = {}, {}
    for sym in SYMBOLS:
        r = fetch_rates(mt5, sym, args.bars, timeframe=args.timeframe)
        if r is None or len(r) < 500:
            continue
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spread, _n = choose_spread(r, info, tick, source="median")
        costs[sym] = float(spread) / float(np.median(r["close"]))
        data[sym] = r
    mt5.shutdown()
    if not data:
        return 1
    cost_rt = float(np.mean(list(costs.values()))) * 2 * 100

    # Collect signals per symbol, both sides.
    per_sym = {}
    for sym, r in data.items():
        o, h, l, c = (r[k].astype(float) for k in ("open", "high", "low", "close"))
        bs, bsig = sequential(o, h, l, c, buy=True)
        ss, ssig = sequential(o, h, l, c, buy=False)
        per_sym[sym] = {"c": c, "buy_setup": bs, "buy_sig": bsig,
                        "sell_setup": ss, "sell_sig": ssig}

    n_setup = sum(len(v["buy_setup"]) + len(v["sell_setup"]) for v in per_sym.values())
    n_sig = sum(len(v["buy_sig"]) + len(v["sell_sig"]) for v in per_sym.values())
    bars = sum(len(v["c"]) for v in per_sym.values())
    print(f"\n  DeMark Sequential, {args.timeframe}, {len(data)} majors, {bars:,} bars")
    print(f"  {n_setup:,} completed 9-bar setups -> {n_sig:,} completed 13-counts "
          f"({n_sig / max(1, n_setup) * 100:.1f}% survive to a signal)")
    print(f"  round trip costs {cost_rt:.4f}% of price")
    print("  " + "-" * 68)
    print(f"  {'horizon':9}{'SETUP only':>14}{'t':>8}{'SEQUENTIAL':>14}{'t':>8}"
          f"{'countdown adds':>16}")

    rows = []
    for hz in args.horizons:
        setup_r, sig_r = [], []
        for v in per_sym.values():
            setup_r.append(forward(v["c"], v["buy_setup"], hz, True))
            setup_r.append(forward(v["c"], v["sell_setup"], hz, False))
            sig_r.append(forward(v["c"], v["buy_sig"], hz, True))
            sig_r.append(forward(v["c"], v["sell_sig"], hz, False))
        su = np.concatenate([x for x in setup_r if len(x)])
        sg = np.concatenate([x for x in sig_r if len(x)])
        adds = (sg.mean() - su.mean()) * 100 if len(sg) else float("nan")
        print(f"  {hz:<9}{su.mean() * 100:>+13.4f}%{tstat(su):>+8.2f}"
              f"{sg.mean() * 100:>+13.4f}%{tstat(sg):>+8.2f}{adds:>+15.4f}%")
        rows.append({"horizon": hz, "n_setup": int(len(su)), "n_signal": int(len(sg)),
                     "setup_pct": float(su.mean() * 100), "setup_t": tstat(su),
                     "signal_pct": float(sg.mean() * 100), "signal_t": tstat(sg),
                     "countdown_adds_pct": float(adds)})

    # Null: circularly shift the CLOSE series against the signal dates, which
    # holds the signal count, the spacing and the clustering exactly and
    # destroys only the alignment with what price then did.
    hz = args.horizons[len(args.horizons) // 2]
    rng = np.random.default_rng(20260923)
    real = []
    for v in per_sym.values():
        real.append(forward(v["c"], v["buy_sig"], hz, True))
        real.append(forward(v["c"], v["sell_sig"], hz, False))
    real = np.concatenate([x for x in real if len(x)])
    real_t = tstat(real)
    null_t = []
    for _ in range(args.null_rounds):
        pooled = []
        for v in per_sym.values():
            n = len(v["c"])
            k = int(rng.integers(50, n - 50))
            shifted = np.roll(v["c"], k)
            pooled.append(forward(shifted, v["buy_sig"], hz, True))
            pooled.append(forward(shifted, v["sell_sig"], hz, False))
        p = np.concatenate([x for x in pooled if len(x)])
        tt = tstat(p)
        if tt == tt:
            null_t.append(tt)
    null_t = np.array(null_t)
    p_two = float((np.abs(null_t) >= abs(real_t)).mean())

    print("  " + "-" * 68)
    print(f"  at horizon {hz}: real t {real_t:+.2f}; shifted-price null puts "
          f"|t| >= {abs(real_t):.2f} in {p_two * 100:.1f}%")
    print(f"  null |t| reaches {np.abs(null_t).max():.2f}, 95th pct "
          f"{np.percentile(np.abs(null_t), 95):.2f}")

    # Cross-market and cross-era, since there is no parameter to sweep.
    print("  " + "-" * 68)
    print(f"  {'symbol':9}{'signals':>9}{'mean %':>10}{'t':>8}")
    by_sym = {}
    for sym, v in per_sym.items():
        parts = [forward(v["c"], v["buy_sig"], hz, True),
                 forward(v["c"], v["sell_sig"], hz, False)]
        s = np.concatenate([x for x in parts if len(x)]) if any(
            len(x) for x in parts) else np.array([])
        by_sym[sym] = {"n": int(len(s)),
                       "mean_pct": float(s.mean() * 100) if len(s) else None,
                       "t": tstat(s) if len(s) else None}
        print(f"  {sym:9}{len(s):>9}{s.mean() * 100 if len(s) else float('nan'):>+10.4f}"
              f"{tstat(s) if len(s) else float('nan'):>+8.2f}")
    pos = sum(1 for v in by_sym.values() if v["mean_pct"] and v["mean_pct"] > 0)
    print(f"  {pos} of {len(by_sym)} symbols positive")

    print(f"\n  the sequential signal is {rows[len(rows) // 2]['signal_pct']:+.4f}% "
          f"at horizon {hz} against a round trip of {cost_rt:.4f}%")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"timeframe": args.timeframe, "bars": bars,
                   "n_setups": n_setup, "n_signals": n_sig,
                   "by_horizon": rows, "by_symbol": by_sym,
                   "symbols_positive": pos,
                   "null": {"horizon": hz, "real_t": real_t,
                            "p_two_sided": p_two, "rounds": len(null_t),
                            "max_abs_t": float(np.abs(null_t).max())},
                   "cost_round_trip_pct": cost_rt}, fh, indent=2)
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
