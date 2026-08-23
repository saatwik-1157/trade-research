"""Import TradingView trade exports and analyse them.

TradingView has no public API for reading your account, charts or trades, so
there is nothing to "connect" to. What it does support is exporting trades to
CSV, and this parses those exports into the same trade schema MetaTrader
produces, so both sources land in one comparable report.

Supported layouts, detected automatically:

1. **Strategy Tester "List of Trades"** - two rows per trade sharing a trade
   number, one "Entry ..." and one "Exit ...". Column names vary by TradingView
   version and by the currency of the instrument (P&L may arrive as "Net P&L",
   "Profit", or "Net P&L USDT"), so columns are matched on substrings rather
   than exact names.
2. **One row per trade** - any export where a single row carries both sides.

Where to get the file:
    Strategy Tester -> List of Trades -> the export/download icon.
    Broker-connected accounts: Trading Panel -> History -> export.

Usage:
    python tools/tv_import.py "C:/path/List of Trades.csv"
    python tools/tv_import.py trades.csv --out reports/tv_trades.json
    python tools/tv_import.py trades.csv --inspect      # show detected columns
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import trade_stats  # noqa: E402

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d %b %Y %H:%M", "%b %d, %Y %H:%M",
]


def parse_date(value: str):
    v = (value or "").strip()
    if not v:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def parse_number(value):
    """Tolerant numeric parse: strips currency, thousands separators, parens."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in {"-", "--", "n/a", "N/A"}:
        return None
    negative = s.startswith("(") and s.endswith(")")  # (123.45) accounting style
    for junk in "()$€£¥%,\u00a0 ":
        s = s.replace(junk, "")
    s = s.replace("\u2212", "-")  # unicode minus
    # Strip a trailing currency code such as "12.34USDT"
    while s and s[-1].isalpha():
        s = s[:-1]
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


def find_column(headers: list[str], *needles: str):
    """First header containing all needles (case-insensitive)."""
    for h in headers:
        low = h.lower()
        if all(n.lower() in low for n in needles):
            return h
    return None


def _classify(headers: list[str]) -> dict:
    return {
        "trade_no": find_column(headers, "trade") or find_column(headers, "#"),
        "type": find_column(headers, "type") or find_column(headers, "side")
                or find_column(headers, "signal") or find_column(headers, "direction"),
        "datetime": find_column(headers, "date") or find_column(headers, "time"),
        "price": find_column(headers, "price"),
        "quantity": find_column(headers, "quantity") or find_column(headers, "qty")
                    or find_column(headers, "size") or find_column(headers, "contracts"),
        "pnl": find_column(headers, "net", "p&l") or find_column(headers, "net", "pnl")
               or find_column(headers, "profit") or find_column(headers, "p&l")
               or find_column(headers, "pnl"),
        "symbol": find_column(headers, "symbol") or find_column(headers, "ticker")
                  or find_column(headers, "instrument"),
        "entry_time": find_column(headers, "entry", "time") or find_column(headers, "open", "time"),
        "exit_time": find_column(headers, "exit", "time") or find_column(headers, "close", "time"),
        "entry_price": find_column(headers, "entry", "price") or find_column(headers, "open", "price"),
        "exit_price": find_column(headers, "exit", "price") or find_column(headers, "close", "price"),
        "commission": find_column(headers, "commission") or find_column(headers, "fee"),
    }


def _direction(text: str) -> str:
    t = (text or "").lower()
    if "short" in t or "sell" in t:
        return "short"
    return "long"


def load(path: str, default_symbol: str | None = None) -> tuple[list[dict], dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(fh, dialect=dialect))

    if not rows:
        raise ValueError(f"{path} contains no data rows")

    headers = list(rows[0].keys())
    cols = _classify(headers)
    meta = {"file": path, "rows": len(rows), "headers": headers, "detected_columns": cols}

    if not cols["pnl"]:
        raise ValueError(
            "No profit/loss column found. Detected headers: "
            + ", ".join(headers)
            + ". Re-export including a P&L column, or pass --inspect to see what was read."
        )

    symbol_default = default_symbol or os.path.splitext(os.path.basename(path))[0]

    paired = bool(cols["trade_no"] and cols["type"]) and any(
        "entry" in str(r.get(cols["type"], "")).lower() for r in rows
    )
    meta["layout"] = "paired entry/exit rows" if paired else "one row per trade"

    trades = []
    if paired:
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(str(r.get(cols["trade_no"], "")).strip(), []).append(r)

        for tno, group in groups.items():
            entry = next((r for r in group if "entry" in str(r.get(cols["type"], "")).lower()), None)
            exit_ = next((r for r in group if "exit" in str(r.get(cols["type"], "")).lower()), None)
            if entry is None or exit_ is None:
                continue  # an open trade, or a clipped export
            # P&L is carried on the exit row in TradingView's layout; fall back
            # to the entry row for exports that put it there instead.
            pnl = parse_number(exit_.get(cols["pnl"]))
            if pnl is None:
                pnl = parse_number(entry.get(cols["pnl"]))
            if pnl is None:
                continue
            t_open = parse_date(entry.get(cols["datetime"], "")) if cols["datetime"] else None
            t_close = parse_date(exit_.get(cols["datetime"], "")) if cols["datetime"] else None
            trades.append({
                "trade_no": tno,
                "symbol": (entry.get(cols["symbol"]) if cols["symbol"] else None) or symbol_default,
                "direction": _direction(entry.get(cols["type"], "")),
                "volume": parse_number(entry.get(cols["quantity"])) if cols["quantity"] else None,
                "entry_price": parse_number(entry.get(cols["price"])) if cols["price"] else None,
                "exit_price": parse_number(exit_.get(cols["price"])) if cols["price"] else None,
                "open_time": t_open.isoformat(timespec="seconds") if t_open else None,
                "close_time": t_close.isoformat(timespec="seconds") if t_close else None,
                "hold_hours": round((t_close - t_open).total_seconds() / 3600, 2)
                              if (t_open and t_close) else None,
                "commission": parse_number(exit_.get(cols["commission"])) if cols["commission"] else None,
                "net_profit": round(pnl, 2),
            })
    else:
        for i, r in enumerate(rows):
            pnl = parse_number(r.get(cols["pnl"]))
            if pnl is None:
                continue
            t_open = parse_date(r.get(cols["entry_time"] or cols["datetime"] or "", ""))
            t_close = parse_date(r.get(cols["exit_time"] or cols["datetime"] or "", ""))
            trades.append({
                "trade_no": str(i + 1),
                "symbol": (r.get(cols["symbol"]) if cols["symbol"] else None) or symbol_default,
                "direction": _direction(r.get(cols["type"], "") if cols["type"] else ""),
                "volume": parse_number(r.get(cols["quantity"])) if cols["quantity"] else None,
                "entry_price": parse_number(r.get(cols["entry_price"] or cols["price"] or "")),
                "exit_price": parse_number(r.get(cols["exit_price"] or cols["price"] or "")),
                "open_time": t_open.isoformat(timespec="seconds") if t_open else None,
                "close_time": t_close.isoformat(timespec="seconds") if t_close else None,
                "hold_hours": round((t_close - t_open).total_seconds() / 3600, 2)
                              if (t_open and t_close) else None,
                "commission": parse_number(r.get(cols["commission"])) if cols["commission"] else None,
                "net_profit": round(pnl, 2),
            })

    trades.sort(key=lambda t: t["close_time"] or "")
    meta["trades_parsed"] = len(trades)
    meta["rows_skipped"] = len(rows) - (len(trades) * 2 if paired else len(trades))
    return trades, meta


def main() -> int:
    ap = argparse.ArgumentParser(description="Import and analyse a TradingView trade export.")
    ap.add_argument("csv_path")
    ap.add_argument("--symbol", help="symbol to use when the export has no symbol column")
    ap.add_argument("--inspect", action="store_true", help="show detected columns and exit")
    ap.add_argument("--out", help="write the full JSON here")
    args = ap.parse_args()

    try:
        trades, meta = load(args.csv_path, args.symbol)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")
        return 1

    if args.inspect:
        print(json.dumps(meta, indent=2))
        return 0

    stats = trade_stats.statistics(trades, source=f"TradingView export ({meta['layout']})")
    result = {"meta": meta, "trades": trades, "statistics": stats}

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"Wrote {args.out}")

    print(f"\nParsed {meta['trades_parsed']} trades from {meta['rows']} rows "
          f"({meta['layout']})")
    print(trade_stats.render(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
