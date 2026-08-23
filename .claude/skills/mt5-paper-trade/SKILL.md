---
name: mt5-paper-trade
description: Run an automated trading rule on a MetaTrader 5 DEMO account, with a hard code-level guard against real accounts, dry-run by default, and position and daily-loss caps. Use only when the user explicitly asks to automate demo trading.
---

# Automated demo trading

```bash
python tools/mt5_paper.py --once                          # dry run, sends nothing
python tools/mt5_paper.py --rule sma_cross --live --once  # one live demo cycle
python tools/mt5_paper.py --rule random --live --interval 300
python tools/mt5_paper.py --close-all --live              # flatten its own positions
```

This is the only module in the project that sends orders, kept separate from
`mt5_account.py`, which reads and cannot write.

## The fence

1. **DEMO accounts only.** `account_info().trade_mode` must be 0. REAL, CONTEST
   and any unrecognised value all abort before an order is constructed. Verified
   against all four cases.
2. **Dry-run by default.** `--live` is required to send anything.
3. **MAGIC-tagged orders**, so it only ever closes positions it opened.
4. **Caps**: `--lot`, `--max-positions`, `--max-daily-loss` (halts the session).

Never relax these to make a run work. If a user asks to point it at a live
account, the answer is that this tool cannot, and a live executor is a separate
decision needing explicit discussion of size, drawdown and failure handling.

## What to tell the user about results

**None of the rules has a measured edge.** This repository backtested its own
composite at IC 0.002 and found zero of 105 tested patterns surviving multiple-
testing correction. Expect roughly break-even before costs and negative after.

The `random` rule is the benchmark, and comparing against it is the point: a
rule that cannot separate itself from coin-flipping over a few hundred trades
has not demonstrated anything. Report that comparison whenever both have run.

Do not tune a rule until the demo looks profitable and then present it as
working. Demo profits are what justify live money, and a curve-fit that survives
a few hundred demo trades will not survive real ones.

## Requirements

- MT5 terminal running, logged into a demo account.
- **Algorithmic trading enabled** for `--live`: Tools -> Options -> Expert
  Advisors -> Allow Algorithmic Trading. Dry runs do not need it.
- Markets open. Forex is closed from Friday evening to Sunday 17:00 New York;
  a closed market reports `no_quote_market_probably_closed`.

Afterwards, analyse with `python tools/mt5_account.py --days 7`.
