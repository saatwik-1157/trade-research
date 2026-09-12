#!/usr/bin/env python
"""Accumulating ledger of live paper trades, with the clustered significance.

The problem this solves is that a broker's history window and a 27-trade
sample both expire. `mt5_account.py` reports whatever MT5 still holds; this
merges each read into `data/track_record.jsonl` keyed by position_id, so the
sample grows monotonically and survives the terminal's history being pruned.
Re-running it is idempotent - a position already on file is left alone.

READ ONLY with respect to trading. Like mt5_account.py it touches only the
MT5 readers; the only things it writes are its own ledger and report.

Two corrections are applied before any figure is quoted, both of them the same
correction this repository applies everywhere else:

  Bracket regime. A trade's magnitude is set by its stop distance, so pooling
  a 3.0xATR-stop / 0.5xATR-target session with a 1.5x/1.5x one adds numbers
  that are not the same quantity - structurally the metals-points error. Net
  currency is therefore reported per regime, and a data gap is raised when the
  regimes disagree by more than 2x. The R-multiple - net over the money
  actually at risk - is the unit that survives pooling, and it is derived from
  each trade's own fill rather than assumed.

  Date clustering. Seven pairs all carrying USD open together on one dollar
  move, so pooling counts one move seven times. See
  rule_search.clustered_by_date, which is reused directly rather than restated.

Usage:
    python tools/track_record.py --merge              # pull from MT5, then report
    python tools/track_record.py                      # report from the ledger only
    python tools/track_record.py --merge --days 30
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths as _paths

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import mt5_account
import mt5_paper
import numpy as np
import rule_search

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _paths.project_root()
LEDGER = os.path.join(ROOT, "data", "track_record.jsonl")
ORDER_LOG = os.path.join(ROOT, "data", "paper_trades.jsonl")

# Ratio of stop distances beyond which two sessions are not one sample.
REGIME_FENCE = 2.0


def load_ledger(path: str = LEDGER) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r["position_id"]] = r
    return out


def order_geometry(path: str = ORDER_LOG) -> dict:
    """Requested sl/tp distances per order ticket, in price units.

    The order log is the only record of the bracket a trade was opened with.
    MT5's deal history keeps the levels but not the ATR multiples, and a
    position closed early never reveals either.
    """
    geo = {}
    if not os.path.exists(path):
        return geo
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("event") != "order" or not r.get("order"):
                continue
            price, sl, tp = r.get("price"), r.get("sl"), r.get("tp")
            if not (price and sl and tp) or sl == price:
                continue
            geo[r["order"]] = {
                "requested_price": price,
                "sl_distance": abs(sl - price),
                "tp_distance": abs(tp - price),
                "reward_risk": round(abs(tp - price) / abs(sl - price), 3),
                "logged_fill": r.get("fill_price"),
                "slippage_points": r.get("slippage_points"),
                "bracket_repaired": bool(r.get("bracket_repaired", False)),
            }
    return geo


def enrich(trade: dict, geo: dict) -> dict:
    """Attach bracket geometry and the R-multiple to one closed trade.

    The value of a price unit is derived from the trade's own gross P&L over
    its own price move, so this needs no contract-size table and no assumption
    about what a lot is worth. A trade that closed exactly at its entry gives
    no denominator and gets a null rather than a guess.
    """
    out = dict(trade)
    g = geo.get(trade["position_id"])
    out["bracket"] = g
    out["r_multiple"] = None
    if not g:
        return out

    # Retroactive form of the check mt5_paper.place now runs at order time.
    # It works on trades opened before that fix existed, because the levels and
    # the actual fill are both on file - so a bracket that was already inverted
    # when it opened is visible here rather than only in a future run.
    is_buy = trade["direction"] == "long"
    lo, hi = sorted((g["requested_price"] - g["sl_distance"],
                     g["requested_price"] + g["sl_distance"]))
    sl_level = hi if is_buy is False else lo
    tp_level = (g["requested_price"] + g["tp_distance"]) if is_buy else (
        g["requested_price"] - g["tp_distance"])
    out["bracket_inverted_at_fill"] = not mt5_paper.bracket_is_sane(
        is_buy, trade["entry_price"], sl_level, tp_level)

    move = abs(trade["exit_price"] - trade["entry_price"])
    if move <= 0 or not trade.get("gross_profit"):
        return out
    value_per_price_unit = abs(trade["gross_profit"]) / move
    risk = g["sl_distance"] * value_per_price_unit
    if risk > 0:
        out["r_multiple"] = round(trade["net_profit"] / risk, 4)
    return out


def census(trades: list[dict]) -> dict[int, int]:
    """How many closed trades each magic opened. The account, not this tool."""
    counts: dict[int, int] = {}
    for t in trades:
        key = int(t.get("magic", 0) or 0)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def select_by_magic(trades: list[dict], magic: int | None) -> list[dict]:
    """The trades one magic opened, or all of them when `magic` is None.

    Split out of `merge` so it can be tested without a terminal, because the
    question it answers -- whose trades is this ledger -- is the one that
    decides what every statistic downstream describes.

    A trade with no magic (0) belongs to nobody in particular: opened by hand in
    the terminal, or by something that does not tag. It is never silently folded
    into this tool's sample.
    """
    if magic is None:
        return list(trades)
    return [t for t in trades if int(t.get("magic", 0) or 0) == magic]


def drop_positions(trades: list[dict], exclude: set[str]) -> list[dict]:
    """Trades minus a list of position ids.

    The magic filter cannot reach a trade opened before the tags were split.
    Nine of the ten trades the platform made at this venue on 2026-09-07 carry
    the harness's own 770315, because the adapter shared it until that evening;
    only 58334342528 is separable by tag. They are separable by IDENTITY -- the
    platform's `positions` table lists exactly which tickets were its -- so this
    takes the ids rather than guessing from a time window.
    """
    if not exclude:
        return list(trades)
    return [t for t in trades if str(t["position_id"]) not in exclude]


def merge(
    days: int,
    path: str | None = None,
    magic: int | None = mt5_paper.MAGIC,
    exclude: set[str] | None = None,
    ) -> dict:
    """Pull closed trades from MT5 and add the ones not already on file.

    **`magic` decides whose trades this ledger is.** `closed_trades` pairs every
    deal on the ACCOUNT, so a trade opened by hand, by the platform's broker
    adapter, or by any other EA arrives here beside this tool's -- and until the
    entry deal's magic was recorded, on 2026-09-07, nothing could tell them
    apart. The sample behind every "no edge" finding in CLAUDE.md was therefore
    "every closed trade on this account", not "every trade this tool made".

    The default is this tool's own tag, which is what the ledger has always
    claimed to be. `None` restores the old behaviour and takes everything.
    Either way the result reports what was left out and under whose tag, so the
    choice is visible rather than assumed.

    Rows already on file are untouched. This changes what is ADDED, not what
    the ledger already contains.
    """
    mt5 = mt5_account.connect(path)
    try:
        # WHICH account these trades came from. A ledger is keyed by position
        # id, and position ids are unique per account rather than globally --
        # so trades from a second demo account merge in cleanly, with nothing
        # to say they are not the same record. Measured 2026-09-07: the ledger
        # held 252 trades from account tickets 101-103 million at net -22.37
        # and 252 from 583 million at +31.87, and reported the sum, +9.50, as
        # one number. Same class as the metals points error, wearing an
        # account number.
        info = mt5.account_info()
        login = int(getattr(info, "login", 0) or 0) or None
        # Server clock at both ends. A local upper bound silently drops every
        # deal the server stamped later and reads as "no trades" - and a local
        # lower bound shortens the window by the same offset.
        until = mt5_paper.history_end(mt5)
        since = mt5_paper.server_now(mt5) - timedelta(days=days)
        trades, _cash = mt5_account.closed_trades(mt5, since, until)
    finally:
        mt5.shutdown()

    seen = census(trades)
    trades = select_by_magic(trades, magic)
    before = len(trades)
    trades = drop_positions(trades, set(exclude or ()))
    dropped = before - len(trades)

    geo = order_geometry()
    ledger = load_ledger()
    added = 0
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        for t in sorted(trades, key=lambda x: x["close_time"]):
            if t["position_id"] in ledger:
                continue
            rec = enrich(t, geo)
            # On the row, so a ledger written before the filter existed can
            # still be separated after the fact.
            rec["magic"] = t.get("magic", 0)
            rec["account"] = login
            rec["merged_at"] = datetime.now().isoformat(timespec="seconds")
            fh.write(json.dumps(rec) + "\n")
            ledger[t["position_id"]] = rec
            added += 1
    return {
        "pulled": len(trades),
        "added": added,
        "ledger_size": len(ledger),
        "magic": magic,
        "account": login,
        "by_magic": dict(sorted(seen.items())),
        "excluded": sum(n for m, n in seen.items() if magic is not None and m != magic),
        "dropped_by_id": dropped,
    }


def _rows(trades: list[dict], field: str) -> list[dict]:
    """Shape trades for the rule_search clustering helpers."""
    rows = []
    for t in trades:
        v = t.get(field)
        if v is None:
            continue
        rows.append({
            "symbol": t["symbol"],
            "net": float(v),
            "entry_time": int(datetime.fromisoformat(t["open_time"]).timestamp()),
        })
    return rows


def significance(rows: list[dict]) -> dict:
    """Pooled t plus both clusterings, and the sample the effect would need."""
    if len(rows) < 2:
        return {"trades": len(rows), "note": "too few trades for inference"}
    net = np.array([r["net"] for r in rows], dtype=float)
    n = len(net)
    mean, sd = float(net.mean()), float(net.std(ddof=1))
    t = mean / (sd / math.sqrt(n)) if sd > 0 else None
    out = {
        "trades": n,
        "mean": round(mean, 4),
        "sd": round(sd, 4),
        "t_stat_pooled": round(t, 2) if t is not None else None,
        "wins": int((net > 0).sum()),
        "win_rate": round(float((net > 0).mean()), 4),
        "total": round(float(net.sum()), 4),
    }
    if t is not None and mean != 0:
        need = int(math.ceil((1.96 * sd / mean) ** 2))
        out["trades_needed_for_t_1_96"] = need
        out["trades_still_needed"] = max(0, need - n)
    out.update(rule_search.clustered_by_date(rows))
    out.update(rule_search.clustered_t(rows))
    return out


def report() -> dict:
    ledger = load_ledger()
    trades = sorted(ledger.values(), key=lambda x: x["close_time"])
    if not trades:
        return {"generated_at": datetime.now().isoformat(timespec="seconds"),
                "trades": 0,
                "data_gaps": ["ledger is empty; run with --merge"]}

    gaps = []

    # Group by bracket regime. Rounding the reward:risk ratio is what makes a
    # session one regime rather than one regime per trade, since the ATR under
    # each order differs even when the multiples do not.
    regimes = {}
    for t in trades:
        b = t.get("bracket")
        key = str(round(b["reward_risk"], 1)) if b and b.get("reward_risk") else "unknown"
        regimes.setdefault(key, []).append(t)

    known = sorted(float(k) for k in regimes if k != "unknown")
    if len(known) > 1 and known[0] > 0 and known[-1] / known[0] > REGIME_FENCE:
        gaps.append(
            f"trades span {len(known)} bracket regimes, reward:risk {known[0]} to "
            f"{known[-1]} ({known[-1] / known[0]:.1f}x); net currency is not one "
            f"quantity across them - quote by_regime or the R-multiple, never the "
            f"pooled net")
    if "unknown" in regimes:
        gaps.append(
            f"{len(regimes['unknown'])} trades have no order-log entry, so their "
            f"bracket and R-multiple are null (opened before logging, or by hand)")

    # WHICH ACCOUNT. A ledger is keyed by position id and position ids are
    # unique per account, not globally, so a second demo account's trades merge
    # in cleanly with nothing to say they are not the same record. Measured
    # 2026-09-07: 252 trades from tickets 101-103 million at net -22.37 sat
    # beside 252 from 583 million at +31.87, and the report printed the sum,
    # +9.50, as one number. Same class as the metals points error.
    accounts = {}
    for t in trades:
        accounts[t.get("account")] = accounts.get(t.get("account"), 0) + 1
    if len(accounts) > 1:
        shown = ", ".join(f"{k if k is not None else 'unrecorded'}: {v}"
                          for k, v in sorted(accounts.items(), key=lambda kv: str(kv[0])))
        gaps.append(
            f"trades come from {len(accounts)} sources ({shown}); a position id is "
            f"unique per account, so these merged without colliding and the pooled "
            f"net adds accounts that are not one sample - quote by_account")

    by_account = {}
    for key in sorted(accounts, key=lambda k: str(k)):
        rows = [t for t in trades if t.get("account") == key]
        by_account[str(key) if key is not None else "unrecorded"] = {
            "trades": len(rows),
            "net": round(sum(x["net_profit"] for x in rows), 2),
            "first_close": min(x["close_time"] for x in rows)[:10],
            "last_close": max(x["close_time"] for x in rows)[:10],
            **significance(_rows(rows, "net_profit")),
        }

    r_rows = _rows(trades, "r_multiple")
    if len(r_rows) < len(trades):
        gaps.append(f"R-multiple available for {len(r_rows)} of {len(trades)} trades")

    by_regime = {}
    for k, v in sorted(regimes.items()):
        sl = [x["bracket"]["sl_distance"] for x in v if x.get("bracket")]
        by_regime[k] = {
            "trades": len(v),
            "net": round(sum(x["net_profit"] for x in v), 2),
            "median_sl_distance": round(float(np.median(sl)), 5) if sl else None,
            **significance(_rows(v, "net_profit")),
        }

    slips = [abs(s) for t in trades
             if (s := (t.get("bracket") or {}).get("slippage_points")) is not None]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger": os.path.relpath(LEDGER, ROOT).replace("\\", "/"),
        "trades": len(trades),
        "first_close": trades[0]["close_time"],
        "last_close": trades[-1]["close_time"],
        "net_currency_total": round(sum(t["net_profit"] for t in trades), 2),
        "accounts": len(accounts),
        "by_account": by_account,
        "by_regime": by_regime,
        "pooled_r_multiple": significance(r_rows),
        "repaired_brackets": sum(
            1 for t in trades if (t.get("bracket") or {}).get("bracket_repaired")),
        "inverted_at_fill": sum(1 for t in trades if t.get("bracket_inverted_at_fill")),
        "max_abs_slippage_points": max(slips) if slips else None,
        "data_gaps": gaps,
    }


def _f(v, width, spec=".2f"):
    """Format a figure that may legitimately be absent."""
    return f"{'-':>{width}}" if v is None else f"{v:>{width}{spec}}"


def render(rep: dict) -> str:
    if not rep.get("trades"):
        return "\n  ledger is empty; run with --merge\n"

    L = ["",
         f"  track record   {rep['trades']} trades   "
         f"{rep['first_close'][:10]} to {rep['last_close'][:10]}   "
         f"net {rep['net_currency_total']:+.2f}",
         f"  ledger: {rep['ledger']}",
         "",
         f"  {'regime R:R':<12}{'trades':>7}{'net':>9}{'win':>7}"
         f"{'t pooled':>10}{'t by date':>11}"]

    for k, v in rep["by_regime"].items():
        wr = v.get("win_rate")
        L.append(f"  {k:<12}{v['trades']:>7}{v['net']:>9.2f}"
                 + (f"{'-':>7}" if wr is None else f"{wr * 100:>6.0f}%")
                 + _f(v.get("t_stat_pooled"), 10)
                 + _f(v.get("t_stat_clustered_by_date"), 11))

    if rep.get("accounts", 1) > 1:
        L += ["", f"  {'account':<14}{'trades':>7}{'net':>9}   window"]
        for k, v in rep["by_account"].items():
            L.append(f"  {k:<14}{v['trades']:>7}{v['net']:>9.2f}   "
                     f"{v['first_close']} to {v['last_close']}")

    r = rep["pooled_r_multiple"]
    L += ["", "  R-multiple (the unit that pools across regimes):"]
    if r.get("trades", 0) >= 2:
        L.append(f"    n={r['trades']}  mean={r['mean']:+.3f}R  win {r['win_rate'] * 100:.0f}%"
                 f"  t={r.get('t_stat_pooled')}"
                 f"  by date={r.get('t_stat_clustered_by_date')}"
                 f"  by symbol={r.get('t_stat_clustered_by_symbol')}")
        if r.get("trades_still_needed") is not None:
            L.append(f"    {r['trades_still_needed']:,} more trades needed to reach "
                     f"pooled t=1.96 at this effect size")
    else:
        L.append(f"    {r.get('note', 'unavailable')}")

    if rep.get("inverted_at_fill"):
        L.append(f"\n  {rep['inverted_at_fill']} trade(s) opened with BOTH exits against "
                 f"the position - bracket priced off a stale quote, so the loss was "
                 f"fixed at order time")
    if rep.get("repaired_brackets"):
        L.append(f"  {rep['repaired_brackets']} trades opened with a repaired bracket")
    if rep.get("max_abs_slippage_points") is not None:
        L.append(f"  worst recorded slippage: {rep['max_abs_slippage_points']:.0f} points")

    if rep["data_gaps"]:
        L.append("")
        L += [f"  gap: {g}" for g in rep["data_gaps"]]

    L += ["",
          "  Live-trade counts this small describe what happened and predict nothing.",
          ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--merge", action="store_true",
                    help="pull closed trades from MT5 into the ledger first")
    ap.add_argument("--days", type=int, default=30, help="history window for --merge")
    ap.add_argument("--terminal", default=None, help="path to terminal64.exe")
    ap.add_argument("--out", default="reports/track_record.json")
    ap.add_argument("--all-magics", action="store_true",
                    help="merge every closed trade on the account, whoever opened it. "
                         "The default takes only this tool's own, which is what the "
                         "ledger has always claimed to be")
    ap.add_argument("--exclude", nargs="*", default=[], metavar="POSITION_ID",
                    help="position ids to keep out of the ledger regardless of tag. "
                         "For trades opened before the two systems' magics were "
                         "split, which no filter can separate by tag")
    args = ap.parse_args()

    if args.merge:
        m = merge(args.days, args.terminal,
                  None if args.all_magics else mt5_paper.MAGIC,
                  {str(x) for x in args.exclude})
        print(f"\n  merged: {m['added']} new of {m['pulled']} pulled, "
              f"ledger now {m['ledger_size']} trades")
        # What the account held and what was taken from it. A ledger that
        # quietly absorbed somebody else's trades is a sample nobody can
        # interpret afterwards, so the choice is printed at the moment it is
        # made rather than left to be inferred from the file.
        who = ", ".join(f"{mg}: {n}" for mg, n in m["by_magic"].items()) or "nothing"
        print(f"  closed on the account, by magic -- {who}")
        if m["magic"] is None:
            print("  --all-magics: EVERY trade was taken, whoever opened it")
        elif m["excluded"]:
            print(f"  excluded {m['excluded']} trade(s) this tool did not open "
                  f"(magic != {m['magic']})")
        if m["dropped_by_id"]:
            print(f"  dropped {m['dropped_by_id']} trade(s) named on --exclude")

    rep = report()
    print(render(rep))

    if args.out:
        dest = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"  wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
