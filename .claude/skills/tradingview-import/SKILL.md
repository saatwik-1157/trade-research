---
name: tradingview-import
description: Import a TradingView trade export (Strategy Tester List of Trades, or broker history CSV) and compute execution statistics in the same format as MetaTrader. Use when asked to analyse TradingView trades, strategy results, or backtest output.
---

# TradingView trade import

## There is no TradingView API

TradingView publishes no public API for reading your account, charts,
indicators or trades. Nothing can be connected to directly, and any library
claiming otherwise is scraping an internal websocket — fragile and against
their terms. Do not suggest one.

The supported routes are CSV export (this skill) and alert webhooks (below).

## Import an export

```bash
python tools/tv_import.py "path/to/List of Trades.csv"
python tools/tv_import.py trades.csv --symbol BTCUSDT --out reports/tv_trades.json
python tools/tv_import.py trades.csv --inspect      # show detected columns
```

Where the file comes from:

- **Strategy Tester → List of Trades → export icon** — backtest or forward-test
  results for a Pine strategy.
- **Trading Panel → History → export** — real trades on a connected broker.

Column names vary by TradingView version and by instrument currency, so
detection matches on substrings. **Run `--inspect` first on any unfamiliar
export** and confirm the detected columns are the intended ones before
reporting numbers off it. If the P&L column is missing the tool refuses rather
than guessing, which is the correct behaviour.

## Interpreting the output

Statistics come from the same engine as the MT5 skill, so the two are directly
comparable rather than two dialects of "win rate".

**Backtest results are not trading results.** If the export is from Strategy
Tester, these figures describe a simulation with perfect fills, no slippage,
and — critically — a strategy that was very likely tuned on this same data.
In-sample backtest performance is the least predictive number in trading. Say
so whenever reporting Strategy Tester output; do not present it as a track
record.

**Sample size governs everything.** Quote `sample_size_assessment` alongside
the numbers. Under about 100 trades, win rate and profit factor are noise.

**Costs.** TradingView commission settings are whatever the strategy author
configured, and often unrealistic or absent. Check whether commission was
modelled at all before treating net profit as achievable.

## Alert webhooks — the other supported route

For live signals rather than history, TradingView alerts can POST JSON to a URL
you control (Pine `alert()`, webhook URL field, paid plan required). That needs
a listener reachable from the internet. Build it only if the user asks; it is a
separate piece of infrastructure and only pays off for someone actually running
Pine strategies.

## Reporting

Lead with net profit, win rate, profit factor, max drawdown and the per-symbol
and per-direction split. State the sample-size assessment. If the source is a
backtest, say that first, before any number.
