#!/usr/bin/env python
"""Crypto OHLCV in the shape `rule_search.py` already consumes.

Four universes have now come back null - seven FX majors, eight crosses, four
metals, seven equity indices - and the honest reading was that the next move is
new DATA rather than new configurations of the same data. This broker carries
no crypto, so the bars come from a public exchange endpoint through ccxt.

The point is emphatically NOT a new backtester. freqtrade, vectorbt,
nautilus_trader, backtrader and lumibot are all better engines than this
repository has, and not one of them carries a permutation null, a Bonferroni
threshold over the true candidate count, era blocks, a walk-forward that
re-ranks, date clustering or a unit-mismatch fence. The engine was never the
scarce part. So this module does one thing - fetch bars and hand them to the
harness that already knows how to disbelieve a result.

Two properties make crypto worth the trouble, and one makes it dangerous.

Worth it: there is no shared dollar leg, so the correlation that inflated the
D1 majors result cannot operate the same way; and the venues run 24/7, so a
"day" has no session structure to accidentally fit.

Dangerous: quote sizes differ wildly. BTC near six figures and a token near
0.0001 are not in the same units, and pooling "points" across them is the exact
arithmetic error the metals search produced. rule_search.py's 5x fence catches
it, and this module reports the spread up front so the choice of universe is
made before the search rather than explained after it.

Fees are not the MT5 spread. A taker fee is charged on notional at both ends,
which at 1.5xATR brackets is a materially different hurdle per symbol; the
--fee-bps figure is converted into a per-trade price cost so the harness
charges something real rather than zero.

    python tools/crypto_market.py --symbols BTC/USDT,ETH/USDT --timeframe 1d
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np


def fetch_ohlcv(exchange, symbol, timeframe, limit, pause=0.25):
    """Page backwards until the venue stops giving more.

    Exchanges cap a single call well below the history available - 1000 bars is
    typical - so a single request silently returns a short window and every
    statistic downstream is computed on it without complaint.
    """
    out, since = [], None
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=1000,
                                     params={"until": since} if since else {})
        if not batch:
            break
        # Oldest first; page backwards from the earliest timestamp seen.
        batch = [b for b in batch if not out or b[0] < out[0][0]]
        if not batch:
            break
        out = batch + out
        since = batch[0][0] - 1
        if len(out) >= limit:
            break
        time.sleep(pause)
    return out[-limit:] if out else []


def to_market(rows, fee_bps):
    """ccxt rows -> the dict rule_search's market map uses.

    `point` is one PERCENT of the symbol's median close, not 1.0. In FX a point
    is a fixed tick and comparable across the majors, which span 2.16x; in
    crypto the same choice puts BTC near six figures beside DOGE near 0.09 and
    the two differ by 232,000x, so a pooled "points" figure adds quantities that
    are not the same quantity - the metals error, several orders of magnitude
    worse. A percent is the same size everywhere, so it is the unit that makes
    this universe poolable at all. Every downstream figure then reads as
    percent of price, and the 5x fence measures real dispersion in volatility
    rather than an artefact of quote size.
    """
    a = np.array(rows, dtype=float)
    t, o, h, l, c = a[:, 0] / 1000.0, a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    point = float(np.median(c)) / 100.0
    # A taker fee is paid on notional at entry and exit; at these bracket sizes
    # the round trip is what a spread would have been.
    spread = float(np.median(c) * (fee_bps / 10000.0) * 2)
    return {"o": o, "h": h, "l": l, "c": c, "time": t.astype("int64"),
            "point": point, "spread": spread, "n": len(c),
            "from": str(np.datetime64(int(t[0]), "s")),
            "to": str(np.datetime64(int(t[-1]), "s"))}


def main():
    ap = argparse.ArgumentParser(description="Crypto OHLCV for the rule_search harness.")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--fee-bps", type=float, default=10.0,
                    help="taker fee in basis points, charged at both ends")
    ap.add_argument("--out")
    args = ap.parse_args()

    import ccxt
    exchange = getattr(ccxt, args.exchange)({"enableRateLimit": True})

    result = {"exchange": args.exchange, "timeframe": args.timeframe,
              "fee_bps": args.fee_bps, "by_symbol": {}, "data_gaps": []}
    scales = {}
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        try:
            rows = fetch_ohlcv(exchange, sym, args.timeframe, args.limit)
        except Exception as exc:                      # noqa: BLE001 - venue errors vary
            result["data_gaps"].append(f"{sym}: {type(exc).__name__}: {exc}")
            continue
        if len(rows) < 300:
            result["data_gaps"].append(f"{sym}: only {len(rows)} bars, too few to cut into eras")
            continue
        m = to_market(rows, args.fee_bps)
        # Dispersion is measured in the unit the harness will actually use.
        scales[sym] = float(np.median(m["h"] - m["l"]) / m["point"])
        result["by_symbol"][sym] = {
            "bars": m["n"], "from": m["from"], "to": m["to"],
            "median_price": round(float(np.median(m["c"])), 6),
            "median_bar_range_pct": round(scales[sym], 3),
            "round_trip_cost_pct": round(m["spread"] / m["point"], 4),
            "cost_as_pct_of_bar_range": round(
                100 * (m["spread"] / m["point"]) / scales[sym], 2),
        }

    if len(scales) > 1:
        spread_ratio = max(scales.values()) / min(scales.values())
        result["bar_range_spread_x"] = round(spread_ratio, 1)
        if spread_ratio > 5:
            result["data_gaps"].append(
                f"symbols differ in typical daily range by {spread_ratio:.1f}x even "
                "measured in percent, which is real volatility dispersion rather "
                "than a quote-size artefact - read per_symbol")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"wrote {args.out}")

    print(f"\n  {args.exchange} {args.timeframe}, taker {args.fee_bps} bps round trip")
    print(f"  {'symbol':12s} {'bars':>6s} {'from':>12s} {'median px':>12s} {'day range':>9s} {'cost/range':>10s}")
    for sym, r in result["by_symbol"].items():
        print(f"  {sym:12s} {r['bars']:6d} {r['from'][:10]:>12s} "
              f"{r['median_price']:12.4f} {r['median_bar_range_pct']:8.2f}% "
              f"{r['cost_as_pct_of_bar_range']:9.2f}%")
    for g in result["data_gaps"]:
        print(f"  GAP  {g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
