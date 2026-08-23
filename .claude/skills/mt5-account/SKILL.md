---
name: mt5-account
description: Read and analyse a MetaTrader 5 account - balance, open positions, and execution statistics over closed trades (win rate, profit factor, expectancy, drawdown). Read-only. Use when asked about MT5 trades, trading performance, or account history.
---

# MetaTrader 5 account analysis

```bash
python tools/mt5_account.py --days 730                    # default window
python tools/mt5_account.py --days 3650 --out reports/mt5.json
```

Requires the MT5 terminal to be **running and logged in**. The tool connects to
the local terminal over IPC; there is no cloud API and no credentials involved.

## Read-only

This tool never calls `order_send` or any other mutating endpoint. It reads.
Analysing a trading record and placing orders are different activities, and
this skill does not do the second one. If asked to place, modify or close a
trade, say that this tooling deliberately cannot, and do not write a script
that does it — confirm with the user first as a separate, explicit decision.

## Interpreting the output

**Trades, not deals.** MT5 logs opening and closing a position as two separate
deals sharing a `position_id`. The tool pairs them first; a win rate computed
over raw deals is roughly double-counted. If a figure here disagrees with the
terminal's own report, this pairing is usually why.

**Sample size governs everything.** `sample_size_assessment` in the output says
how much weight the statistics carry. Under about 100 trades, win rate and
profit factor are dominated by luck. Quote that assessment whenever you quote
the numbers — a 70% win rate over 20 trades is not evidence of an edge, and
presenting it without the caveat is the core failure this project exists to
avoid.

**Costs are partial.** Spread is embedded in fill prices and never appears as a
line item. Only broker-booked commission and swap show up explicitly, so true
cost is higher than `total_commission + total_swap`.

**Balance operations are excluded.** Deposits and withdrawals appear under
`cash_movements`, not in the trade statistics. Folding them in would corrupt
every ratio.

## Common failures

- `IPC timeout` — the terminal is not running, or the package could not attach.
  The tool retries with an explicit path; pass `--path` if the terminal is
  installed somewhere non-standard.
- `No account is logged in` — the terminal is open but has no account. Log in
  through the terminal UI.
- Zero trades on a demo account is the normal state of a fresh install, not an
  error. Say so plainly rather than reporting a performance summary of nothing.

## Reporting

Lead with net profit, win rate, profit factor and max drawdown, then the
per-symbol and per-direction breakdown. Then state the sample-size assessment.
Do not extrapolate from the record to future expectation, and do not recommend
position sizes or strategy changes off a small sample.
