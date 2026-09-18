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
5. **The platform's Risk Engine rules on every opening order.** `place()` will
   not send one live without an `Approval` from `app.risk.engine`, obtained
   through `risk_gate`. Until 2026-09-11 this path imported nothing from
   `app/` and was the only path that had ever traded -- the audit's first
   critical finding. A closing order is evaluated and RECORDED but never
   refused, because a limit that bounds the risk of opening must not be the
   reason a position cannot be shut.

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

import mt5_retcodes
import paths as _paths

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import numpy as np
import risk_gate

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
    _paths.project_root(), "data", "paper_trades.jsonl"
)


class VenueUnreadable(RuntimeError):
    """The terminal did not answer a read. NOT the same as an empty result.

    The MetaTrader5 package returns `None` from a failed call and an empty
    tuple from a successful one that found nothing, and `x or []` collapses
    the two. Every risk limit in this file is computed from a read, so a
    collapsed disconnect is a limit evaluated against no data -- which passes.
    Raised so that a caller has to decide, rather than inheriting a zero.
    """


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
    got = mt5.positions_get()
    if got is None:
        # An uncountable account is UNKNOWN, not flat. Reporting zero here
        # would read as "nothing is open" and re-open the full book.
        raise VenueUnreadable("positions_get returned None; the open book is unknown")
    return [p for p in got if p.magic == magic]



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
    deals = mt5.history_deals_get(start, history_end(mt5))
    if deals is None:
        # The docstring above has always said what happens when this goes
        # wrong: "returns 0.0 rather than an error, so --max-daily-loss
        # silently stops being a limit at all". Observed 2026-09-10, when a
        # partial disconnect left positions_get answering and account_info
        # returning None for 23 consecutive passes. Fail closed.
        raise VenueUnreadable("history_deals_get returned None; today's P&L is unknown")
    return sum(d.profit + d.commission + d.swap for d in deals if d.magic == MAGIC)


def closed_outcomes(mt5, magic: int = MAGIC) -> list[float]:
    """Net result per CLOSED position today, oldest close first.

    Grouped by `position_id` rather than counted per deal: one position
    produces an entry deal and an exit deal, and a streak counted over deals
    would score every trade twice and call the entry -- which books no
    profit -- a break-even.

    A position is closed only when it has a deal with `entry == DEAL_ENTRY_OUT`.
    An open position also has an entry deal sitting in history at profit 0, and
    admitting it would put a trade that has not finished into a record of how
    the finished ones went.
    """
    start = server_day_start(mt5)
    deals = mt5.history_deals_get(start, history_end(mt5))
    if deals is None:
        raise VenueUnreadable(
            "history_deals_get returned None; the recent outcomes are unknown"
        )

    out_flag = getattr(mt5, "DEAL_ENTRY_OUT", 1)
    nets: dict[int, float] = {}
    closed_at: dict[int, float] = {}
    finished: set[int] = set()
    for d in deals:
        if getattr(d, "magic", None) != magic:
            continue
        pid = getattr(d, "position_id", None)
        if pid is None:
            continue
        nets[pid] = nets.get(pid, 0.0) + d.profit + d.commission + d.swap
        if getattr(d, "entry", None) == out_flag:
            finished.add(pid)
            closed_at[pid] = max(closed_at.get(pid, 0.0), float(d.time))

    return [nets[p] for p in sorted(finished, key=lambda p: closed_at.get(p, 0.0))]


def consecutive_losses(outcomes: list[float]) -> int:
    """How many losses in a row end the sequence.

    A break-even (0.0) BREAKS the streak rather than extending it. A trade
    that cost nothing is not evidence that the rule is failing, and counting
    it as one would trip the pause on a quiet run of scratches.
    """
    n = 0
    for net in reversed(outcomes):
        if net < 0:
            n += 1
        else:
            break
    return n


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
          live: bool, risk_usd: float | None = None, magic: int = MAGIC,
          *, gate=None) -> dict:
    """Construct and send ONE opening order, after the Risk Engine agrees.

    `gate` is the fence added at P2 and it has exactly three states:

      * a `risk_gate.Gate` -- the harness path. The engine rules on the order
        as constructed, with the venue figures the caller observed, and
        nothing is sent unless it approves.
      * `risk_gate.APPROVED_UPSTREAM` -- the platform path. `app/brokers/mt5.py`
        arrives here from the OMS, which creates nothing without an `Approval`
        that only `RiskEngine` can build. Re-evaluating would assess a
        different snapshot against the harness's limits and could refuse an
        order already approved and booked.
      * `None` -- no risk decision exists. A dry run proceeds and says so; a
        LIVE order is refused. Unknown is not permission, and the whole finding
        this parameter closes was an order path with no engine on it.
    """
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

    # -------------------------------------------------------- the fence
    # Evaluated on the order as it will actually be sent -- the sized lot, the
    # real quote, the rounded bracket -- and before the dry-run branch, so a
    # dry run reports the decision a live run would have got. Deciding on the
    # requested figures instead would approve one order and send another.
    bracket_sl = round(sl, info.digits) if bracketed else 0.0
    bracket_tp = round(tp, info.digits) if bracketed else 0.0
    point = float(getattr(info, "point", 0.0) or 0.0)
    spread_points = round((tick.ask - tick.bid) / point, 1) if point else None

    risk_record = None
    if gate is risk_gate.APPROVED_UPSTREAM:
        risk_record = {"engine": "RiskEngine", "kind": "open",
                       "decision": "approved upstream",
                       "reason": "Path B: the OMS holds the Approval"}
    elif gate is not None:
        decision = gate.open(
            symbol=symbol, side=side, volume=lot,
            entry_price=price, stop_loss=bracket_sl, take_profit=bracket_tp,
            risk_amount=(sizing or {}).get("risk_actual"),
            spread_points=spread_points,
        )
        risk_record = decision.record
        if not decision.approved:
            # A refusal is logged like an order, because a veto that leaves no
            # trace is indistinguishable from a check that never ran.
            refusal = {
                "symbol": symbol, "side": side, "lot": lot, "price": price,
                "sl": bracket_sl, "tp": bracket_tp,
                "status": "RISK_HALT" if decision.halt else "RISK_VETO",
                "risk_reason": decision.reason[:300],
                "risk": risk_record,
            }
            _log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "event": "order", **refusal})
            return refusal
    elif live:
        refusal = {
            "symbol": symbol, "side": side, "lot": lot, "price": price,
            "status": "RISK_GATE_MISSING",
            "risk_reason": "a live order needs a risk decision and none was supplied",
        }
        _log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "event": "order", **refusal})
        return refusal

    filling = filling_for(mt5, info)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": bracket_sl,
        "tp": bracket_tp,
        "deviation": 20,
        "magic": magic,
        "comment": "trade-research demo",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    if not live:
        return {"symbol": symbol, "side": side, "status": "DRY_RUN",
                "price": price, "sl": request["sl"], "tp": request["tp"], "lot": lot,
                **({"sizing": sizing} if sizing else {}),
                **({"risk": risk_record} if risk_record else {})}

    # UNKNOWN IS NOT REJECTED, AND THE DIFFERENCE DECIDES WHETHER A RETRY IS
    # SAFE. This call was unguarded, and `status` below reads
    # `"SENT" if done else "REJECTED"` -- so an IPC error raised here, or a
    # `None` returned by a terminal that had gone away, was recorded as though
    # the VENUE had refused the order. It is the opposite claim. A refusal
    # means nothing was transmitted and a retry is free; an exception means
    # the request may well have reached the server and a retry may open a
    # second position. `backend/app/oms/state.py` already draws this line and
    # the harness path was not honouring it.
    send_error = None
    try:
        res = mt5.order_send(request)
    except Exception as exc:  # noqa: BLE001 - recorded as UNKNOWN, never refused
        res = None
        send_error = f"{type(exc).__name__}: {exc}"
    if res is None and send_error is None:
        send_error = "order_send returned None; the terminal did not answer"

    done = getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
    ticket = getattr(res, "order", None)

    # The fill, not the quote. Recording `price` as the entry is what hid the
    # bracket inversion for three days: the log said 0.59752 and the server
    # said 0.59473, and only the account report disagreed.
    fill = float(getattr(res, "price", 0.0) or 0.0) if done else 0.0

    # THE VOLUME THE VENUE FILLED, which is not necessarily the volume asked
    # for. `lot` below is the REQUEST; `res.volume` is what the server
    # actually executed, and on a partial fill they differ. The request was
    # the only one recorded, so a partial fill entered the ledger as a
    # complete one at the full size -- the same "a figure the tool chose is
    # not a figure the server confirmed" defect as reading the quote instead
    # of the fill, in the one field position size is derived from.
    #
    # Recorded beside `lot` rather than replacing it: the pair is the
    # evidence that a fill was partial, and collapsing them would hide the
    # very thing this is for.
    filled = float(getattr(res, "volume", 0.0) or 0.0) if done else 0.0

    out = {
        "symbol": symbol, "side": side, "lot": lot, "price": price,
        "filled_lot": filled or None,
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
        "status": ("SENT" if done
                   else "UNKNOWN" if send_error is not None
                   else "REJECTED"),
        **({"send_error": send_error} if send_error else {}),
        **({"sizing": sizing} if sizing else {}),
        **({"risk": risk_record} if risk_record else {}),
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
        # NO_CHANGES IS NOT A FAILED REPAIR. 10025 means the venue already
        # held the levels being asked for, so the bracket is correct and
        # there is nothing to escape from. Treating anything that is not DONE
        # as a failure sent the emergency close on a position whose stops
        # were fine -- and it happened: EURUSD, 2026-09-07, the one 10025 in
        # the ledger, closed for no reason.
        #
        # This is the case the taxonomy was written for. A boolean "did it
        # work" cannot express "it did not need to", and that gap cost a
        # position.
        repair = mt5_retcodes.classify(fix, action="modify")
        if repair.cls in (mt5_retcodes.Cls.DONE, mt5_retcodes.Cls.MOOT):
            out["sl"], out["tp"] = round(new_sl, info.digits), round(new_tp, info.digits)
            out["bracket_repair_moot"] = repair.cls is mt5_retcodes.Cls.MOOT
        else:
            # Could not fix it and cannot leave it: both exits are against the
            # position, so holding is a guaranteed loss with no upside branch.
            #
            # Recorded through the engine as the close it is, and NOT gated:
            # `record_close` cannot refuse, which is what makes it safe to put
            # in front of the one order that exists to escape a guaranteed
            # loss. See risk_gate.record_close.
            out["risk_close"] = risk_gate.record_close(
                symbol=symbol, side="sell" if is_buy else "buy", volume=lot,
                entry_price=fill,
            ).record
            # THE RESULT OF THIS CLOSE IS THE WHOLE POINT OF IT.
            #
            # It was discarded, and `bracket_repair_failed_closed = True` was
            # then set unconditionally -- so a close the venue REFUSED was
            # recorded as a close that happened, on the one order that exists
            # to escape a guaranteed loss. The position stayed open with both
            # exits against it and the record said it had been dealt with,
            # which is the worst direction for this particular lie to run.
            #
            # Same rule as the flush: a retcode is what the venue said about
            # the request, so record what it said and let the flag mean it.
            escape_error = None
            try:
                escape = mt5.order_send({
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
            except Exception as exc:  # noqa: BLE001 - UNKNOWN, never "closed"
                escape = None
                escape_error = f"{type(exc).__name__}: {exc}"

            escaped = getattr(escape, "retcode", None) == mt5.TRADE_RETCODE_DONE
            out["bracket_repair_failed_closed"] = escaped
            out["bracket_repair_close_retcode"] = getattr(escape, "retcode", None)
            if escape_error:
                out["bracket_repair_close_error"] = escape_error
            if not escaped:
                # Loud, because nothing downstream can infer it. The position
                # is open, both of its exits lose, and no retry happens here.
                print(f"  THE ESCAPE CLOSE FAILED on {symbol} #{ticket}: "
                      f"retcode={getattr(escape, 'retcode', None)}"
                      f"{' ' + escape_error if escape_error else ''}. The position "
                      f"is OPEN with an inverted bracket and every branch a loss. "
                      f"Close it by hand.", flush=True)

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
    except Exception:
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
        # Evaluated and recorded, never refused. `record_close` is written so
        # that nothing it does can stop a close -- an unimportable engine or an
        # unexpected exception both return an approval whose record says the
        # evaluation did not happen. The `--flat-by` flush runs through here,
        # and the flush is the one mechanism bounding a losing tail overnight;
        # it does not get a new way to fail.
        #
        # No `account_id`: `close_own` is given a MAGIC, and a magic is a tag
        # this tool puts on its own orders, not an account number. Writing one
        # into the account field would put a wrong identifier into the audit
        # record, which is a defect this repository has now made three times
        # under three different names. An absent field is a gap; a plausible
        # wrong one is worse.
        risk_close = risk_gate.record_close(
            symbol=p.symbol, side="sell" if is_long else "buy",
            volume=float(p.volume), entry_price=req["price"],
        )
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
               "status": "CLOSED" if done else "FAILED",
               "risk": risk_close.record}
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

    # Consecutive losses PAUSE new entries; they do not halt the session.
    # The distinction is the whole point. A halt would stop managing what is
    # already open and skip the wind-down, so a losing streak would leave the
    # positions it produced sitting unmanaged -- which is the failure the
    # flush exists to prevent, reached by a different road.
    #
    # And it is a pause rather than a size change on purpose. The response to
    # a losing streak is to stop opening, never to open bigger: that is
    # martingale, and it is forbidden outright.
    streak = 0
    cap = getattr(args, "max_consecutive_losses", 0) or 0
    if cap > 0:
        streak = consecutive_losses(closed_outcomes(mt5))

    # The session's risk configuration, told what the venue just said. Both
    # figures are the ones the checks above already acted on, so the engine and
    # this function cannot disagree about a threshold -- only about scope: the
    # checks above decide whether to keep RUNNING, the engine decides about an
    # ORDER. `consecutive_losses` is None when the cap is off, because it was
    # not counted, and a count that did not happen is never zero.
    gate = risk_gate.gate_for(args, getattr(args, "account", None))
    gate.observe(
        positions=open_now,
        realised_today=pnl_today,
        consecutive_losses=streak if cap > 0 else None,
    )

    actions = []
    if cap > 0 and streak >= cap:
        return {
            "halted": False,
            "paused": True,
            "reason": f"{streak} consecutive losses (cap {cap}); new entries paused",
            "realised_today": round(pnl_today, 2),
            "consecutive_losses": streak,
            "open_positions": len(open_now),
            "actions": [
                {"symbol": s_, "status": "skipped_consecutive_losses"}
                for s_ in args.symbols
            ],
        }
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
        result = place(mt5, symbol, side, args.lot, args.sl_atr,
                       args.tp_atr, args.live,
                       getattr(args, "risk_usd", None), gate=gate)
        actions.append(result)

        # A halt is the engine saying the SESSION must stop, not just this
        # order: a breached daily loss, weekly loss or drawdown would be
        # breached by the next order too. Stop proposing immediately rather
        # than working through the remaining symbols to collect identical
        # refusals.
        if result.get("status") == "RISK_HALT":
            return {"halted": True,
                    "reason": f"risk engine: {result.get('risk_reason', '')}"[:300],
                    "realised_today": round(pnl_today, 2),
                    "consecutive_losses": streak,
                    "open_positions": len(open_now), "actions": actions}

        # The gate's view of the book, kept current WITHIN the pass. Without
        # this, seven symbols evaluated against one start-of-pass snapshot
        # would each be told the position count was what it was before any of
        # them opened -- the same accounting the loop above already does for
        # `max_positions`, applied to the engine's copy of it.
        if result.get("status") == "SENT":
            gate.open_positions = (gate.open_positions or 0) + 1
            gate.open_symbols = tuple(gate.open_symbols) + (symbol,)

    return {"halted": False, "paused": False,
            "realised_today": round(pnl_today, 2),
            "consecutive_losses": streak,
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
    ap.add_argument("--max-consecutive-losses", type=int, default=0,
                    metavar="N",
                    help="pause NEW entries after N losing trades in a row "
                         "(0 = off). Open positions keep being managed and "
                         "the wind-down still runs; nothing is ever sized up")
    ap.add_argument("--max-daily-loss", type=float, default=500.0)
    ap.add_argument("--max-spread-points", type=float, default=None,
                    metavar="POINTS",
                    help="refuse an entry when the spread is wider than this. "
                         "Off by default. The harness has measured the spread "
                         "on every cycle since the spread work and handed it "
                         "to the engine, which had the check and no limit to "
                         "check against, so every decision reported it "
                         "not_enforced. Median runs 3 points on EURUSD and 8 "
                         "on NZDUSD; server hour 00 is about 4x normal")
    ap.add_argument("--max-risk-per-trade", type=float, default=None,
                    metavar="USD",
                    help="refuse an order whose stop would cost more than this. "
                         "Off by default: it is NOT the same number as "
                         "--risk-usd, and setting it there would be a check "
                         "that cannot fail. What it catches is the min-lot "
                         "floor, where the volume step forces more risk than "
                         "the budget asked for")
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

    args.account = acct

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
