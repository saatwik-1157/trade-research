"""Execution statistics for a list of closed round-trip trades.

Source-agnostic on purpose. MetaTrader 5 and TradingView describe trades in
completely different shapes, but once each is normalised to the schema below
they get identical treatment, so numbers from the two are directly comparable
rather than two dialects of "win rate" that quietly mean different things.

Required per trade:
    symbol, direction ("long"/"short"), net_profit (float, after costs)
Optional but used when present:
    open_time, close_time (ISO strings), volume, entry_price, exit_price,
    hold_hours, commission, swap
"""
from __future__ import annotations


def drawdown(curve: list[float]) -> dict:
    """Peak-to-trough decline of the cumulative P&L curve."""
    if not curve:
        return {"max_drawdown_currency": None, "max_drawdown_pct_of_peak": None}
    peak, worst, worst_pct = curve[0], 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = v - peak
        if dd < worst:
            worst = dd
            worst_pct = dd / peak if peak > 0 else 0.0
    return {
        "max_drawdown_currency": round(worst, 2),
        "max_drawdown_pct_of_peak": round(worst_pct, 4) if worst_pct else 0.0,
    }


def streaks(flags: list[bool]) -> tuple[int, int]:
    best_win = best_loss = cur_win = cur_loss = 0
    for f in flags:
        if f:
            cur_win, cur_loss = cur_win + 1, 0
        else:
            cur_loss, cur_win = cur_loss + 1, 0
        best_win, best_loss = max(best_win, cur_win), max(best_loss, cur_loss)
    return best_win, best_loss


def _group(trades: list[dict], key: str) -> dict:
    out: dict[str, dict] = {}
    for t in trades:
        bucket = out.setdefault(str(t.get(key, "unknown")), {"trades": 0, "net": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["net"] += t["net_profit"]
        bucket["wins"] += 1 if t["net_profit"] > 0 else 0
    for bucket in out.values():
        bucket["net"] = round(bucket["net"], 2)
        bucket["win_rate"] = round(bucket["wins"] / bucket["trades"], 4)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["trades"]))


def statistics(trades: list[dict], source: str = "unknown") -> dict:
    """Execution statistics over closed round-trip trades."""
    if not trades:
        return {
            "source": source,
            "trade_count": 0,
            "note": "No closed round-trip trades in the requested window. "
                    "Nothing can be concluded about performance.",
        }

    pnl = [float(t["net_profit"]) for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))

    curve, run = [], 0.0
    for p in pnl:
        run += p
        curve.append(run)
    best_w, best_l = streaks([p > 0 for p in pnl])

    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    holds = sorted(t["hold_hours"] for t in trades if t.get("hold_hours") is not None)
    times = [t.get("close_time") for t in trades if t.get("close_time")]

    stats = {
        "source": source,
        "trade_count": len(trades),
        "first_close": min(times) if times else None,
        "last_close": max(times) if times else None,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(pnl) - len(wins) - len(losses),
        "win_rate": round(len(wins) / len(pnl), 4),
        "net_profit": round(sum(pnl), 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        # Below 1.0 means the losing trades outweighed the winning ones
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "expectancy_per_trade": round(sum(pnl) / len(pnl), 2),
        "average_win": avg(wins),
        "average_loss": avg(losses),
        "payoff_ratio": round(abs(avg(wins) / avg(losses)), 3) if wins and losses else None,
        "largest_win": round(max(pnl), 2),
        "largest_loss": round(min(pnl), 2),
        "max_consecutive_wins": best_w,
        "max_consecutive_losses": best_l,
        **drawdown(curve),
        "median_hold_hours": holds[len(holds) // 2] if holds else None,
        "total_commission": round(sum(t.get("commission") or 0 for t in trades), 2),
        "total_swap": round(sum(t.get("swap") or 0 for t in trades), 2),
        "by_symbol": _group(trades, "symbol"),
        "by_direction": _group(trades, "direction"),
    }

    # Sample-size honesty. Win rate and profit factor on a few dozen trades are
    # dominated by luck; stating that beside the numbers is the difference
    # between a statistic and a conclusion.
    n = len(trades)
    if n < 30:
        confidence = ("Far too few trades to distinguish skill from luck. These figures "
                      "describe what happened, and predict nothing.")
    elif n < 100:
        confidence = ("Small sample. Win rate and profit factor here carry wide error bars "
                      "and should not be read as evidence of an edge.")
    elif n < 300:
        confidence = ("Moderate sample. Large effects may be real; small differences between "
                      "symbols or directions are still likely noise.")
    else:
        confidence = ("Reasonable sample size, though results remain specific to the market "
                      "regime these trades were taken in.")

    stats["sample_size_assessment"] = confidence
    stats["caveats"] = [
        "Spread is embedded in fill prices; only broker-booked commission and swap "
        "appear as explicit costs.",
        "Deposits, withdrawals and other balance operations are excluded from these "
        "statistics and reported separately where the source provides them.",
        "Past execution statistics describe a realised sample and do not establish "
        "an expectation for future trades.",
    ]
    return stats


def render(stats: dict) -> str:
    """Plain-text summary for terminal output."""
    if not stats.get("trade_count"):
        return f"\n{stats.get('note', 'No trades.')}\n"

    lines = [
        f"\n{stats['trade_count']} closed trades   "
        f"{(stats['first_close'] or '')[:10]} to {(stats['last_close'] or '')[:10]}"
        f"   [source: {stats['source']}]\n"
    ]
    rows = [
        ("net profit", f"{stats['net_profit']:,.2f}"),
        ("win rate", f"{stats['win_rate']:.1%}  ({stats['wins']}W / {stats['losses']}L)"),
        ("profit factor", str(stats["profit_factor"])),
        ("expectancy / trade", f"{stats['expectancy_per_trade']:,.2f}"),
        ("average win / loss", f"{stats['average_win']} / {stats['average_loss']}"),
        ("payoff ratio", str(stats["payoff_ratio"])),
        ("largest win / loss", f"{stats['largest_win']:,.2f} / {stats['largest_loss']:,.2f}"),
        ("max consecutive L", str(stats["max_consecutive_losses"])),
        ("max drawdown", f"{stats['max_drawdown_currency']:,.2f}"),
        ("median hold", f"{stats['median_hold_hours']} h" if stats["median_hold_hours"] is not None else "n/a"),
    ]
    lines += [f"  {k:<20} {v}" for k, v in rows]

    lines.append(f"\n  {'symbol':<14}{'trades':>8}{'win rate':>11}{'net':>14}")
    for sym, d in list(stats["by_symbol"].items())[:15]:
        lines.append(f"  {sym:<14}{d['trades']:>8}{d['win_rate']:>10.1%}{d['net']:>14,.2f}")

    lines.append(f"\n  {stats['sample_size_assessment']}\n")
    return "\n".join(lines)
