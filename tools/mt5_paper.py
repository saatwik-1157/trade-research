"""Automated trading on a MetaTrader 5 DEMO account.

This is the one module in the project that sends orders. It is separated from
everything else deliberately: `mt5_account.py` reads and can never write, and
this file writes and is fenced so it can only ever reach play money.

The fence, in order of execution:

1. `account_info().trade_mode` must be DEMO. A real or contest account aborts
   before a single order is constructed. This is a check in code, not a rule in
   a prompt, because a prompt cannot fail closed.
2. Orders are dry-run by default. `--live` is required to actually send, and
   even then only after the demo check has passed.
3. Every order carries MAGIC so this tool can identify its own positions and
   will never modify or close one a human opened.
4. Hard caps: lot size, concurrent positions, and a daily loss limit that stops
   trading for the session when breached.

What to expect from it
----------------------
It will lose money on plenty of trades, and roughly break even before costs and
lose after them. That is not a bug to tune away. This repository measured the
composite score at an information coefficient of 0.002 and found zero of 105
tested patterns surviving correction, so none of the rules below has a measured
edge, and the `random` rule exists to make that concrete: if a "real" strategy
cannot separate itself from coin-flipping over a few hundred trades, that is the
finding.

Tuning a rule until the demo looks profitable is the failure mode this whole
project was built to catch. Demo profits justify live money, and the edge that
was never there stops being hypothetical at that point.

Usage:
    python tools/mt5_paper.py --once                       # dry run, no orders
    python tools/mt5_paper.py --rule sma_cross --live --once
    python tools/mt5_paper.py --rule random --live --interval 300
    python tools/mt5_paper.py --close-all --live           # flatten its own positions
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import numpy as np  # noqa: E402

DEFAULT_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
MAGIC = 770315  # tags orders from this tool so it never touches anything else

# Callers that are NOT this tool pass their own tag. `app/brokers/mt5.py` does,
# because sharing one number made each system able to close the other's
# positions: on 2026-09-07 this harness harvested two positions the platform had
# opened, at >= $0.50, and the platform's rows went stale as a result.
#
# The functions below take `magic` so ONE implementation of order construction
# still serves both -- which is the whole reason the adapter calls them rather
# than writing its own send. What separates the two systems is the tag, not the
# code.
TRADE_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_trades.jsonl"
)


class RefuseToTrade(RuntimeError):
    """Raised when the safety fence blocks execution."""


def connect(path: str | None = None):
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:  # optional, Windows-only: keep the rest usable without it
        raise RefuseToTrade(
            "MetaTrader5 is not installed. It is an optional, Windows-only "
            "dependency: pip install MetaTrader5"
        ) from exc

    if not mt5.initialize(path=path or DEFAULT_TERMINAL, timeout=60000):
        if not mt5.initialize(timeout=60000):
            raise RefuseToTrade(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


def assert_demo(mt5, live: bool = False) -> dict:
    """Abort unless this is unambiguously a demo account.

    trade_mode: 0 DEMO, 1 CONTEST, 2 REAL. Anything that is not 0 - including a
    value this build does not recognise - refuses. Failing closed on the unknown
    case is the whole point of a fence.

    The terminal's algo-trading switch is only required for `live`, because a
    dry run sends nothing. Demanding it earlier would push someone to enable
    order sending just to preview signals, which is precisely backwards.
    """
    ai = mt5.account_info()
    if ai is None:
        raise RefuseToTrade("No account is logged in to the terminal.")
    if ai.trade_mode != 0:
        kind = {1: "CONTEST", 2: "REAL"}.get(ai.trade_mode, f"UNKNOWN({ai.trade_mode})")
        raise RefuseToTrade(
            f"Account {ai.login} on {ai.server} is a {kind} account. "
            "This tool only runs on DEMO accounts and will not place an order here."
        )
    if live and not getattr(mt5.terminal_info(), "trade_allowed", False):
        raise RefuseToTrade(
            "Algorithmic trading is disabled in the terminal. Enable it at "
            "Tools -> Options -> Expert Advisors -> Allow Algorithmic Trading."
        )
    return {
        "login": ai.login, "server": ai.server,
        "currency": getattr(ai, "currency", "?"),
        "balance": getattr(ai, "balance", 0.0),
        "equity": getattr(ai, "equity", 0.0),
        "mode": "DEMO",
    }


# ------------------------------------------------------------------ rules
# Each returns "buy", "sell" or None from the closed bars only. Signals never
# read the forming bar, which would be lookahead against live prices.

def rule_sma_cross(rates) -> str | None:
    c = rates["close"][:-1]
    if len(c) < 60:
        return None
    fast_now, slow_now = c[-20:].mean(), c[-50:].mean()
    fast_prev, slow_prev = c[-21:-1].mean(), c[-51:-1].mean()
    if fast_prev <= slow_prev and fast_now > slow_now:
        return "buy"
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "sell"
    return None


def rule_rsi_reversion(rates, n: int = 14) -> str | None:
    c = rates["close"][:-1]
    if len(c) < 3 * n:
        return None
    d = np.diff(c)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag, al = gain[:n].mean(), loss[:n].mean()
    for g, l in zip(gain[n:], loss[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return None
    rsi = 100 - 100 / (1 + ag / al)
    if rsi < 30:
        return "buy"
    if rsi > 70:
        return "sell"
    return None


def rule_random(rates) -> str | None:
    """Coin flip. The benchmark every other rule has to beat to mean anything."""
    return random.choice(["buy", "sell", None, None])


RULES = {"sma_cross": rule_sma_cross, "rsi_reversion": rule_rsi_reversion, "random": rule_random}


def atr_from(rates, n: int = 14) -> float | None:
    h, l, c = rates["high"][:-1], rates["low"][:-1], rates["close"][:-1]
    if len(c) < n + 2:
        return None
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    return float(tr[-n:].mean())


def _log(record: dict) -> None:
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def own_positions(mt5, magic: int = MAGIC):
    return [p for p in (mt5.positions_get() or []) if p.magic == magic]



def server_now(mt5) -> datetime:
    """The terminal's own clock, which is not the local one.

    Deal timestamps come from the broker's server. Bounding a history query
    with local `datetime.now()` silently drops everything the server stamped
    later than the local clock reads - which, on a server running ahead, is
    every trade closed today. The failure is invisible: the query succeeds and
    returns fewer deals, so the caller reports "no trades" rather than an
    error. A live tick carries the server's clock, so ask it.
    """
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
        tick = mt5.symbol_info_tick(sym)
        if tick and getattr(tick, "time", 0):
            return datetime.fromtimestamp(tick.time)
    return datetime.now()


def history_end(mt5) -> datetime:
    """Upper bound for a history query, padded past both clocks.

    Padding forward cannot pull in deals that do not exist yet, so it is the
    safe direction to be wrong in.
    """
    return max(datetime.now(), server_now(mt5)) + timedelta(days=1)


def server_day_start(mt5) -> datetime:
    """Midnight on the SERVER's clock, in the frame history queries use.

    server_now() renders the broker's stamp through the local timezone, and
    that is correct for BOUNDING a query: the MT5 package reads a naive bound
    the same way, so the offset cancels on both sides. It is not correct for
    finding a day boundary. Calling .replace(hour=0) on it lands on midnight of
    the local-rendered clock, which is the server's midnight shifted by this
    machine's UTC offset - so on a UTC+5:30 machine --max-daily-loss counted
    from server 18:30 the previous day, while the same code on a UTC machine
    counted from midnight. A risk limit whose day moves with the operator's
    timezone is the kind of defect that only shows up on someone else's laptop.

    So: read the stamp as the server's wall clock, take midnight there, then
    render that instant back into the query frame.
    """
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
        tick = mt5.symbol_info_tick(sym)
        if tick and getattr(tick, "time", 0):
            wall = datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(tzinfo=None)
            midnight = wall.replace(hour=0, minute=0, second=0, microsecond=0)
            return datetime.fromtimestamp(calendar.timegm(midnight.timetuple()))
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def realised_today(mt5) -> float:
    """P&L booked by this tool today, used for the daily loss limit.

    "Today" is the server's day, not the local one, and the window runs past
    the server clock. Getting this wrong returns 0.0 rather than an error, so
    --max-daily-loss silently stops being a limit at all.
    """
    start = server_day_start(mt5)
    deals = mt5.history_deals_get(start, history_end(mt5)) or []
    return sum(d.profit + d.commission + d.swap for d in deals if d.magic == MAGIC)


def bracket_is_sane(is_buy: bool, fill: float, sl: float, tp: float) -> bool:
    """A stop must sit against the position and a target with it.

    For a long that means sl < fill < tp, and for a short tp < fill < sl. The
    check is needed because sl and tp are computed from the quote read just
    BEFORE order_send and are transmitted as absolute levels, while the fill
    comes back from the server. A fill far enough from that quote lands outside
    its own bracket, which puts both exits on the same side of entry and makes
    every outcome a loss - the position is closed at a loss the moment it opens
    and the retcode still reads DONE, so nothing in the log looks wrong.

    Position 10200315596 is the worked example: NZDUSD sell quoted at 0.59752
    with tp 0.59638, filled at 0.59473, closed on that tp for -165 points.
    """
    if is_buy:
        return sl < fill < tp
    return tp < fill < sl


def lot_for_risk(mt5, symbol: str, sl_distance: float, risk_amount: float) -> dict:
    """Size a position so its stop costs `risk_amount`, or refuse to size it.

    A fixed lot makes every trade a different bet. A 1.5xATR stop is not the
    same number of points on EURUSD as on USDJPY and a point is not worth the
    same money in either, so 0.01 lots everywhere risks a different sum on
    every symbol - structurally the metals-points error wearing a lot size.
    Sizing off the stop distance is what makes the R-multiple in
    track_record.py a measured quantity rather than a derived one.

    It does not improve expectancy and cannot. Volume is a positive multiplier
    on the per-trade result: it scales +0.021R and -1.00R by the same factor
    and leaves the sign alone. The reason to do it is that the record becomes
    comparable across symbols, not that it earns anything.

    Returns {"lot": float, ...} or {"lot": None, "gap": reason}. It refuses
    rather than falling back to a default, on swap.py's reasoning - a lot size
    guessed from a missing tick value is a real order for the wrong amount.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return {"lot": None, "gap": "no symbol_info"}

    tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    if tick_value <= 0 or tick_size <= 0:
        return {"lot": None,
                "gap": f"no tick value/size for {symbol} "
                       f"(value={tick_value}, size={tick_size})"}
    if sl_distance <= 0:
        return {"lot": None, "gap": "stop distance is not positive"}

    # Loss in account currency if a 1.0-lot position runs to its stop.
    per_lot = (sl_distance / tick_size) * tick_value
    if per_lot <= 0:
        return {"lot": None, "gap": "computed risk per lot is not positive"}

    step = float(getattr(info, "volume_step", 0.0) or 0.01)
    vmin = float(getattr(info, "volume_min", 0.0) or step)
    vmax = float(getattr(info, "volume_max", 0.0) or 0.0)

    raw = risk_amount / per_lot
    # Round the step count before flooring. per_lot is built from a float ATR,
    # so a budget worth exactly 5 steps arrives as 4.999999999 and a bare
    # floor() drops a whole step - the same size for a 5-step order and a
    # 4-step one, with nothing in the record to show which happened.
    lot = math.floor(round(raw / step, 6)) * step
    lot = max(lot, vmin)
    if vmax > 0:
        lot = min(lot, vmax)
    lot = round(lot, 8)

    out = {"lot": lot, "risk_requested": round(risk_amount, 4),
           "risk_actual": round(lot * per_lot, 4),
           "risk_per_lot": round(per_lot, 4)}

    # The min-lot floor is the case that actually bites: at 0.01 minimum a
    # small risk budget cannot be expressed, and the order silently risks more
    # than asked. Report it rather than pretend the sizing held.
    if out["risk_actual"] > risk_amount * 1.5:
        out["gap"] = (f"min lot {vmin} risks {out['risk_actual']:.2f} against a "
                      f"{risk_amount:.2f} budget - volume step is the binding "
                      f"constraint, not the risk setting")
    return out


def place(mt5, symbol: str, side: str, lot: float, sl_atr: float, tp_atr: float,
          live: bool, risk_usd: float | None = None, magic: int = MAGIC) -> dict:
    info = mt5.symbol_info(symbol)
    if info is None and not mt5.symbol_select(symbol, True):
        return {"symbol": symbol, "status": "symbol_unavailable"}
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not tick or tick.ask <= 0 or tick.bid <= 0:
        return {"symbol": symbol, "status": "no_quote_market_probably_closed"}

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
    atr = atr_from(rates) if rates is not None and len(rates) > 40 else None
    if not atr:
        return {"symbol": symbol, "status": "insufficient_history_for_atr"}

    is_buy = side == "buy"
    price = tick.ask if is_buy else tick.bid
    # A multiple of zero means NO bracket, not a bracket of zero width.
    #
    # `price - 0.0 * atr` is `price`, so computing it unconditionally sends
    # sl == tp == entry, which the server refuses with 10016 INVALID_STOPS --
    # and, if it had not, `bracket_is_sane` would have called it insane, the
    # repair would have recomputed the same zero width, and the fallback would
    # have closed the position the moment it opened.
    #
    # MT5 reads 0.0 as "not set", which is what a caller supplying its own
    # absolute levels afterwards needs. `app.brokers.mt5.MT5Adapter.place_order`
    # is that caller: it passes 0.0/0.0 and applies the platform's levels with
    # a follow-up modify, so order construction stays in one place. Measured
    # 2026-09-07, the first time the adapter was ever pointed at a terminal.
    bracketed = sl_atr > 0 and tp_atr > 0
    sl = price - sl_atr * atr if is_buy else price + sl_atr * atr
    tp = price + tp_atr * atr if is_buy else price - tp_atr * atr

    sizing = None
    if risk_usd:
        sizing = lot_for_risk(mt5, symbol, sl_atr * atr, risk_usd)
        if sizing["lot"] is None:
            return {"symbol": symbol, "side": side, "status": "unsized",
                    "gap": sizing["gap"]}
        lot = sizing["lot"]

    filling = filling_for(mt5, info)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": round(sl, info.digits) if bracketed else 0.0,
        "tp": round(tp, info.digits) if bracketed else 0.0,
        "deviation": 20,
        "magic": magic,
        "comment": "trade-research demo",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    if not live:
        return {"symbol": symbol, "side": side, "status": "DRY_RUN",
                "price": price, "sl": request["sl"], "tp": request["tp"], "lot": lot,
                **({"sizing": sizing} if sizing else {})}

    res = mt5.order_send(request)
    done = getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
    ticket = getattr(res, "order", None)

    # The fill, not the quote. Recording `price` as the entry is what hid the
    # bracket inversion for three days: the log said 0.59752 and the server
    # said 0.59473, and only the account report disagreed.
    fill = float(getattr(res, "price", 0.0) or 0.0) if done else 0.0
    point = getattr(info, "point", 0.0) or 0.0

    out = {
        "symbol": symbol, "side": side, "lot": lot, "price": price,
        "fill_price": fill or None,
        "slippage_points": round((fill - price) / point, 1) if (fill and point) else None,
        "sl": request["sl"], "tp": request["tp"],
        "retcode": getattr(res, "retcode", None),
        "comment": getattr(res, "comment", None),
        "order": ticket,
        # The EXECUTION's ticket, distinct from the order's. An order is the
        # instruction and a deal is what the venue did about it, and only the
        # deal identifies one execution uniquely -- which is what a fill has to
        # be deduplicated by, because two genuine partial fills of the same size
        # at the same price are a real thing that happens.
        "deal": int(getattr(res, "deal", 0) or 0) or None,
        "status": "SENT" if done else "REJECTED",
        **({"sizing": sizing} if sizing else {}),
    }

    # Repair rather than close. Closing only ever fires on adverse slippage, so
    # it would bias the record the tool exists to measure; re-anchoring keeps
    # the ATR geometry the rule actually specifies. The flag is the point - a
    # repaired trade entered at a price its signal never saw, and analysis
    # needs to be able to drop it.
    if (
        done
        and fill
        and bracketed
        and not bracket_is_sane(is_buy, fill, request["sl"], request["tp"])
    ):
        new_sl = fill - sl_atr * atr if is_buy else fill + sl_atr * atr
        new_tp = fill + tp_atr * atr if is_buy else fill - tp_atr * atr
        fix = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": ticket,
            "sl": round(new_sl, info.digits),
            "tp": round(new_tp, info.digits),
        })
        out["bracket_repaired"] = True
        out["bracket_repair_retcode"] = getattr(fix, "retcode", None)
        if getattr(fix, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            out["sl"], out["tp"] = round(new_sl, info.digits), round(new_tp, info.digits)
        else:
            # Could not fix it and cannot leave it: both exits are against the
            # position, so holding is a guaranteed loss with no upside branch.
            mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(lot),
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": ticket,
                "price": tick.bid if is_buy else tick.ask,
                "deviation": 20,
                "magic": MAGIC,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            })
            out["bracket_repair_failed_closed"] = True

    _log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "order", **out})
    return out



def filling_for(mt5, info):
    """Pick an order filling mode the symbol actually accepts.

    symbol_info().filling_mode is a bitmask over SYMBOL_FILLING_* (FOK=1,
    IOC=2). The ORDER_FILLING_* request constants are a different enumeration
    (FOK=0, IOC=1, RETURN=2), so the bitmask bits must be translated, not used
    directly - conflating them sends IOC to a FOK-only symbol and the server
    rejects it with retcode 10030.
    """
    mask = getattr(info, "filling_mode", 0) or 0
    if mask & 1:
        return mt5.ORDER_FILLING_FOK
    if mask & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def deal_money(mt5, deal_ticket) -> dict:
    """The money a deal realised, read back from the server's own history.

    MT5 books `profit`, `swap` and `commission` separately and the account
    receives their sum. Returning them apart as well as together keeps the
    composition visible -- a trade that looks flat on profit and negative on
    swap is a financing cost, not a losing trade, and the distinction is one
    this repository has already had to make elsewhere.

    Every value is None when the deal cannot be read. The deal usually appears
    in history immediately after `order_send`, but "usually" is not "always",
    and inventing the figure would defeat the point of asking.
    """
    empty = {"profit": None, "swap": None, "commission": None, "deal": None}
    if not deal_ticket:
        return empty
    try:
        deals = mt5.history_deals_get(ticket=deal_ticket)
    except Exception:  # noqa: BLE001 - an unreadable history is a gap, not a crash
        return empty
    if not deals:
        return empty
    d = deals[0]
    profit = float(getattr(d, "profit", 0.0) or 0.0)
    swap = float(getattr(d, "swap", 0.0) or 0.0)
    commission = float(getattr(d, "commission", 0.0) or 0.0)
    return {
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "net": round(profit + swap + commission, 2),
        "deal": int(deal_ticket),
    }


def close_own(mt5, live: bool, where=None, magic: int = MAGIC) -> list[dict]:
    """Close positions this tool opened, optionally only those matching `where`.

    The predicate takes an MT5 position and returns a bool. It exists so a
    caller can harvest on floating P&L without duplicating the order
    construction, which is the part that has already cost this project a live
    bug (see filling_for and bracket_is_sane).
    """
    out = []
    for p in own_positions(mt5, magic):
        if where is not None and not where(p):
            continue
        tick = mt5.symbol_info_tick(p.symbol)
        if not tick:
            out.append({"ticket": p.ticket, "status": "no_quote"})
            continue
        is_long = p.type == 0
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price": tick.bid if is_long else tick.ask,
            "deviation": 20,
            "magic": magic,
            "comment": "trade-research close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_for(mt5, mt5.symbol_info(p.symbol)),
        }
        if not live:
            out.append({"ticket": p.ticket, "symbol": p.symbol, "status": "DRY_RUN"})
            continue
        res = mt5.order_send(req)
        done = getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
        # The FILL, not the quote -- the same rule `place` learned the hard way.
        # Recording only a retcode says the request was accepted and says
        # nothing about what was actually taken off, so a caller cannot tell a
        # completed close from an acknowledged one. `app.brokers.mt5` needs
        # exactly this to confirm a close instead of parking it `unknown`, and
        # without it every close through the platform needed reconciliation by
        # hand. Measured 2026-09-07: position 58328592839 closed at 1.16350 for
        # +0.33 while the platform recorded it as unresolved.
        rec = {"ticket": p.ticket, "symbol": p.symbol,
               "retcode": getattr(res, "retcode", None),
               # The venue's ticket for the CLOSE order itself. Recorded even
               # when the close failed, and that is the case it exists for: a
               # close whose outcome is unknown can only be settled by asking
               # the venue about it BY id, and without this the platform held
               # no identifier to ask with. Distinct from `deal` below -- an
               # order is the instruction, a deal is the execution.
               "order": int(getattr(res, "order", 0) or 0) or None,
               "fill_price": float(getattr(res, "price", 0.0) or 0.0) if done else None,
               "closed_volume": float(getattr(res, "volume", 0.0) or 0.0) if done else None,
               "requested_volume": float(p.volume),
               "status": "CLOSED" if done else "FAILED"}
        # What the ACCOUNT received, in account currency, as the server booked
        # it -- not a price difference. A close of 0.01 EURUSD from 1.16319 to
        # 1.16315 is -0.04 USD; the arithmetic `(exit - entry) * lots` gives
        # -0.0000004, because it omits the contract size, and there is no single
        # multiplier that fixes it for FX, metals and indices at once. The
        # server already knows the answer, so it is read rather than derived.
        #
        # Absent when the deal is not yet in history. Reported as None rather
        # than filled in from the arithmetic: an unknown figure is a gap, and a
        # gap is safer than a plausible wrong number.
        rec.update(deal_money(mt5, getattr(res, "deal", None)) if done else
                   {"profit": None, "swap": None, "commission": None, "deal": None})
        _log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "close", **rec})
        out.append(rec)
    return out


def cycle(mt5, args) -> dict:
    open_now = own_positions(mt5)
    pnl_today = realised_today(mt5)

    if pnl_today <= -abs(args.max_daily_loss):
        return {"halted": True, "reason": f"daily loss limit hit ({pnl_today:.2f})",
                "realised_today": round(pnl_today, 2)}

    actions = []
    for symbol in args.symbols:
        if len(open_now) + len([a for a in actions if a.get("status") in ("SENT", "DRY_RUN")]) >= args.max_positions:
            actions.append({"symbol": symbol, "status": "skipped_max_positions"})
            continue
        if any(p.symbol == symbol for p in open_now):
            actions.append({"symbol": symbol, "status": "skipped_already_open"})
            continue
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
        if rates is None or len(rates) < 60:
            actions.append({"symbol": symbol, "status": "no_history"})
            continue
        side = RULES[args.rule](rates)
        if side is None:
            actions.append({"symbol": symbol, "status": "no_signal"})
            continue
        actions.append(place(mt5, symbol, side, args.lot, args.sl_atr,
                            args.tp_atr, args.live,
                            getattr(args, "risk_usd", None)))

    return {"halted": False, "realised_today": round(pnl_today, 2),
            "open_positions": len(open_now), "actions": actions}


def main() -> int:
    ap = argparse.ArgumentParser(description="Automated trading on a MetaTrader 5 DEMO account.")
    ap.add_argument("--rule", choices=sorted(RULES), default="sma_cross")
    ap.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--risk-usd", type=float, default=None,
                    help="size each trade so its stop costs this much; "
                         "overrides --lot. Does not change expectancy.")
    ap.add_argument("--sl-atr", type=float, default=1.5, help="stop loss in ATR multiples")
    ap.add_argument("--tp-atr", type=float, default=1.5, help="take profit in ATR multiples")
    ap.add_argument("--max-positions", type=int, default=3)
    ap.add_argument("--max-daily-loss", type=float, default=500.0)
    ap.add_argument("--interval", type=int, default=0, help="seconds between cycles; 0 runs once")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--live", action="store_true", help="actually send orders (demo only)")
    ap.add_argument("--close-all", action="store_true", help="close positions this tool opened")
    ap.add_argument("--path", default=None)
    args = ap.parse_args()
    args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        mt5 = connect(args.path)
        acct = assert_demo(mt5, live=args.live)
    except RefuseToTrade as exc:
        print(f"\n  REFUSED: {exc}\n")
        return 1

    print(f"\n  account {acct['login']} @ {acct['server']}  [{acct['mode']}]  "
          f"balance {acct['balance']:,.2f} {acct['currency']}")
    print(f"  rule={args.rule}  symbols={','.join(args.symbols)}  lot={args.lot}  "
          f"sl={args.sl_atr}xATR  tp={args.tp_atr}xATR")
    print(f"  mode: {'LIVE ORDERS (demo account)' if args.live else 'DRY RUN - no orders sent'}\n")

    try:
        if args.close_all:
            for r in close_own(mt5, args.live):
                print(f"  close {r['symbol']} #{r['ticket']}: {r['status']}")
            return 0

        while True:
            res = cycle(mt5, args)
            stamp = datetime.now().strftime("%H:%M:%S")
            if res["halted"]:
                print(f"  [{stamp}] HALTED: {res['reason']}")
                break
            print(f"  [{stamp}] open={res['open_positions']} "
                  f"realised_today={res['realised_today']:.2f}")
            for a in res["actions"]:
                detail = f"{a.get('side','')} @ {a.get('price','')}" if a.get("side") else ""
                print(f"      {a['symbol']:<10}{a['status']:<32}{detail}")
            if args.once or not args.interval:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  stopped by user")
    finally:
        mt5.shutdown()

    print(f"\n  order log: {TRADE_LOG}")
    print("  Analyse results with: python tools/mt5_account.py --days 7\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
