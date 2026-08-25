#!/usr/bin/env python
"""Overnight financing, in the same points the rest of the project speaks.

`simulate()` prices the spread and nothing else. That was defensible while the
rules were intraday, but every D1 result in this repository holds positions
across server midnight, and six of the seven indices measured charge swap on
BOTH sides - you pay to be long and you pay to be short. An unmodelled cost
that is always negative does not average out; it biases every slow-timeframe
result upward, including the near-survivors.

Two things have to be right for a swap number to mean anything here.

The unit. MT5 reports swap in one of several systems and they are not
interchangeable - the same defect the `median_atr_points_by_symbol` warning
exists to catch. `trade_tick_value` is already expressed in the deposit
currency, so a per-lot cost in deposit currency divides straight into points.
Where the broker quotes swap in the symbol's base currency instead, the rate
that converts it is the symbol's own price whenever the quote side IS the
deposit currency, which covers every XXXUSD pair on this account. Anything
needing a third instrument to convert is NOT guessed: it returns None with a
reason, and the caller reports a data gap. Filling that from memory is the
exact failure this project exists to prevent.

The count. Swap accrues per server midnight crossed, not per bar and not per
24 hours, and one weekday is billed triple to carry the weekend. Counting
midnights on the local clock, or forgetting the triple day, understates the
bill - and understating cost is the direction that manufactures an edge.

    python tools/swap.py --symbols EURUSD,US500,DE40,JPN225
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# ENUM_SYMBOL_SWAP_MODE. Only the modes that convert exactly are handled; the
# interest and reopen modes carry different semantics and are refused rather
# than approximated.
POINTS = 0
CURRENCY_SYMBOL = 1
CURRENCY_MARGIN = 2
CURRENCY_DEPOSIT = 3

MODE_NAMES = {0: "POINTS", 1: "CURRENCY_SYMBOL", 2: "CURRENCY_MARGIN",
              3: "CURRENCY_DEPOSIT", 4: "INTEREST_CURRENT", 5: "INTEREST_OPEN",
              6: "REOPEN_CURRENT", 7: "REOPEN_BID"}

# ENUM_DAY_OF_WEEK
DOW = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
       5: "Friday", 6: "Saturday"}


def value_per_point(info):
    """Deposit-currency value of one point, for one lot.

    trade_tick_value is per tick, and a tick is not always a point - scaling by
    point/tick_size is what makes the two comparable.
    """
    if not info.trade_tick_size:
        return None
    return info.trade_tick_value * (info.point / info.trade_tick_size)


def swap_points_per_night(info, price, deposit_currency):
    """(long_points, short_points, note). Points are negative when they cost.

    Returns (None, None, reason) rather than a guess when the broker's unit
    cannot be converted without a third instrument. A caller that gets None
    must record a data gap, not substitute zero - zero swap is a claim that
    holding overnight is free, which is measurably false on this account.
    """
    mode = info.swap_mode
    lng, sht = info.swap_long, info.swap_short

    if mode == POINTS:
        # Already the unit we want.
        return float(lng), float(sht), None

    vpp = value_per_point(info)
    if not vpp:
        return None, None, f"{info.name}: no tick size, cannot value a point"

    if mode == CURRENCY_DEPOSIT:
        per_lot = 1.0
    elif mode in (CURRENCY_SYMBOL, CURRENCY_MARGIN):
        cur = info.currency_base if mode == CURRENCY_SYMBOL else info.currency_margin
        if cur == deposit_currency:
            per_lot = 1.0
        elif info.currency_profit == deposit_currency and price:
            # Quote side is the deposit currency, so one unit of the base is
            # worth `price` of it - the symbol converts its own swap.
            per_lot = float(price)
        else:
            return (None, None,
                    f"{info.name}: swap quoted in {cur}, deposit is "
                    f"{deposit_currency}, and {info.name} does not quote that "
                    "rate - conversion needs a third instrument")
    else:
        return (None, None,
                f"{info.name}: swap mode {MODE_NAMES.get(mode, mode)} is not a "
                "flat per-night charge and is not converted here")

    return float(lng) * per_lot / vpp, float(sht) * per_lot / vpp, None


def nights_between(t_entry, t_exit, triple_dow):
    """Server midnights crossed, counting the triple-rollover day three times.

    Both bounds are epoch seconds on the SERVER clock. The midnight that lands
    on `triple_dow` carries the weekend, so it bills three nights; missing that
    understates a Wednesday-to-Thursday hold by two nights on every FX major.
    """
    if t_exit <= t_entry:
        return 0
    first = (int(t_entry) // 86400) + 1          # first midnight strictly after entry
    last = int(t_exit) // 86400                  # last midnight at or before exit
    nights = 0
    for day in range(first, last + 1):
        # Epoch day 0 was a Thursday; ENUM_DAY_OF_WEEK counts Sunday as 0.
        dow = (day + 4) % 7
        nights += 3 if dow == triple_dow else 1
    return nights


# A night's financing that runs to a tenth of the instrument's daily range
# would dominate every other term in a trade, which no real instrument does.
# Output that large means the broker's unit was read wrong, not that holding is
# ruinous - so it is refused. Metals quoted "in base currency" are the case
# that motivated this: reading the charge as ounces of gold and converting at
# the gold price inflates it by roughly the price of gold.
IMPLAUSIBLE_FRACTION_OF_ATR = 0.10


def implausible(points, atr_points):
    if points is None or not atr_points:
        return False
    return abs(points) > IMPLAUSIBLE_FRACTION_OF_ATR * atr_points


def describe(mt5, symbols, atr_by_symbol=None):
    ai = mt5.account_info()
    deposit = getattr(ai, "currency", "USD")
    atr_by_symbol = atr_by_symbol or {}
    out, gaps = {}, []
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            gaps.append(f"{sym}: could not select")
            continue
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        if info is None:
            gaps.append(f"{sym}: no symbol info")
            continue
        price = getattr(tick, "bid", 0.0) or 0.0
        lng, sht, note = swap_points_per_night(info, price, deposit)
        atr = atr_by_symbol.get(sym)
        if implausible(lng, atr) or implausible(sht, atr):
            worst = max(abs(lng or 0), abs(sht or 0))
            note = (f"{sym}: converted swap of {worst:,.0f} points a night is "
                    f"{worst / atr:.0%} of its median daily range - the broker's "
                    f"unit for swap_mode {MODE_NAMES.get(info.swap_mode, info.swap_mode)} "
                    "is not what this conversion assumes, so no figure is reported")
            lng = sht = None
        if note:
            gaps.append(note)
        out[sym] = {
            "swap_mode": MODE_NAMES.get(info.swap_mode, info.swap_mode),
            "swap_long_raw": info.swap_long,
            "swap_short_raw": info.swap_short,
            "triple_day": DOW.get(info.swap_rollover3days, info.swap_rollover3days),
            "long_points_per_night": round(lng, 2) if lng is not None else None,
            "short_points_per_night": round(sht, 2) if sht is not None else None,
            "costs_both_sides": bool(lng is not None and lng < 0 and sht < 0),
        }
    return {"deposit_currency": deposit, "by_symbol": out, "data_gaps": gaps}


def main():
    ap = argparse.ArgumentParser(description="Overnight financing, converted to points.")
    ap.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,USDCHF,AUDUSD,USDCAD,NZDUSD")
    ap.add_argument("--out")
    ap.add_argument("--path")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import numpy as np
    from rule_backtest import atr_series, connect, fetch_rates

    mt5 = connect(args.path)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    # The plausibility fence needs a scale to judge against, and the daily
    # range is the natural one: swap is charged per night.
    atr_by_symbol = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            continue
        rates = fetch_rates(mt5, sym, 3000, "D1", min_bars=100)
        info = mt5.symbol_info(sym)
        if rates is None or info is None:
            continue
        a = atr_series(rates["high"].astype(float), rates["low"].astype(float),
                       rates["close"].astype(float))
        atr_by_symbol[sym] = float(np.nanmedian(a) / info.point)

    result = describe(mt5, symbols, atr_by_symbol)
    result["median_atr_points_by_symbol"] = {k: round(v, 1) for k, v in atr_by_symbol.items()}
    mt5.shutdown()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"wrote {args.out}")

    print(f"\n  deposit currency: {result['deposit_currency']}")
    print(f"  {'symbol':9s} {'mode':18s} {'long/night':>11s} {'short/night':>12s}  triple")
    for sym, row in result["by_symbol"].items():
        lp, sp = row["long_points_per_night"], row["short_points_per_night"]
        f = lambda v: f"{v:>11.2f}" if v is not None else f"{'gap':>11s}"
        print(f"  {sym:9s} {row['swap_mode']:18s} {f(lp)} {f(sp):>12s}  {row['triple_day']}")
    for g in result["data_gaps"]:
        print(f"  GAP  {g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
