# Live trading runbook

The operational sequence. **It cannot be completed today** — steps 8–11 and 20
fail on this deployment, for the reasons in
`LIVE_TRADING_READINESS_REPORT.md`. It is written as the procedure that will
apply, with each step's actual command.

Every command below is read-only except where marked.

## 1–5 Infrastructure

```bash
docker compose up -d                       # WRITES: starts the stack
docker ps --format '{{.Names}}\t{{.Status}}'
curl -s http://127.0.0.1:8000/health        | python -m json.tool
curl -s http://127.0.0.1:8000/health/ready  | python -m json.tool
```

Expect `tr-postgres`, `tr-redis`, `tr-api`, `tr-frontend`, `tr-nginx` up;
`/health/ready` reporting database, redis and workers healthy;
`critical_unavailable: []`.

`/health` also prints `live_execution_blockers`. **A non-empty list ends the
runbook here.**

## 6 Market data

```bash
curl -s http://127.0.0.1:8000/v1/monitoring/status
```

Freshness for the dashboard is `MARKET_DATA_STALE_SECONDS` (900s). The live gate
uses `LIVE_QUOTE_MAX_AGE_SECONDS` (60s) instead: a bar fifteen minutes old is
fine to look at and not fine to trade on.

## 7 TradingView

The gateway refuses every alert when `TV_WEBHOOK_SECRET` is unset, which is the
correct default. Export it in the shell that starts the stack; never commit it.

```bash
TV_WEBHOOK_SECRET=... docker compose up -d api    # WRITES
```

## 8 MT5 terminal

Confirm the terminal is running and algorithmic trading is enabled:

```bash
python -c "import MetaTrader5 as m; m.initialize(); print(m.terminal_info().trade_allowed)"
```

`True` means ready. `False` is the Algo Trading toggle — Tools → Options →
Expert Advisors → Allow Algorithmic Trading. Read-only calls keep working with
it off, so a healthy account read is not evidence that trading is enabled.

## 9–10 Broker account and identity

```bash
python tools/mt5_account.py
```

Read **broker, server, account number, account type, currency, balance, equity,
free margin, leverage** off the terminal and compare each against
`LIVE_EXPECTED_ACCOUNT_ID` / `LIVE_EXPECTED_SERVER`.

**A mismatch stops the runbook.** Do not switch accounts, and do not guess which
is correct.

## 11 Symbols

Every live symbol needs: it exists at the venue, the internal→venue mapping is
right, bid and ask are present, the timestamp is fresh, the spread is inside its
limit, and the market is open.

## 12 Strategies

Only strategies whose status is approved/active/live, each pinned to a version
id **and** a configuration fingerprint, each defining a stop and a target. A
position that cannot be protected must not be entered.

## 13 AI models

If the seat is enabled: model id, version, fingerprint, approval state, and a
defined deterministic fallback. If it is disabled, that is a pass — no opinion
is not approval, and it is not a refusal either.

## 14–17 Risk, sizing, OMS, positions

```bash
curl -s http://127.0.0.1:8000/v1/risk/limits
curl -s http://127.0.0.1:8000/v1/risk/status
curl -s http://127.0.0.1:8000/v1/orders?status=unknown
curl -s http://127.0.0.1:8000/v1/positions
```

All eight capital limits must be set: approved capital, risk budget, max daily
loss, max drawdown, max exposure, max position size, max open positions, max
daily orders. **Any one unset is a blocker** — an unset limit is an unenforced
one.

## 18 Monitoring

`MONITORING_ENABLED=true` and the collection worker running. A live session
nobody is watching is one whose first failure is discovered from the balance.

## 19 Kill switches

Reachable at global, account and strategy scope, and none engaged.

## 20 Preflight

```bash
cd backend && python -m app.live.preflight --strict
```

Run it **on the Windows host** — the API image has no `MetaTrader5` package.
Exit 0 and a final line of `READY_FOR_LIVE` is the only acceptable result.

## 21 Broker state

No unresolved order, no order in `unknown`, no position at the venue the
platform does not know about, positions and orders reconciled.

An unexpected position is **flagged, never closed, never adopted**.

## 22 Arm, then activate

Two distinct acts by a named person, each with a reason:

- `arm()` — refuses a report that did not pass. **Arming places no order.**
- `activate()` — requires `ARMED` *and* a fresh passing report. The one `arm()`
  saw is evidence about when `arm()` ran.

`DISABLED → ACTIVE` is not a legal move and never will be.

## 23 Watch the first execution

Do not force one. Wait for a legitimate approved strategy signal through the
normal pipeline. Follow it by `execution_id`, which is minted once and travels
through the risk decision, the sizing result, the order intent, the logs and the
events.

## 24 Reconcile

```bash
curl -s http://127.0.0.1:8000/v1/brokers/{account_id}/reconcile
```

Broker state against internal state. Any discrepancy stops new entries.

## 25–26 Journal, portfolio, analytics

Confirm the trade reached the journal, the portfolio updated, and analytics
attribute it to the right account, strategy, bot, symbol, model and timeframe.

## Stopping

Deactivation stops **new entries**. It does not close positions: disabling a bot
does not make its positions disappear, and automatic flattening is not the
policy. Reconcile, verify no unintended pending order remains, record the
session end.

## Emergency

Emergency stop blocks new entries and new submissions, preserves the audit
trail, alerts, and transitions to a safe state. It has one exit and it is
downward — returning to trading means walking the whole path again from
`DISABLED`.
