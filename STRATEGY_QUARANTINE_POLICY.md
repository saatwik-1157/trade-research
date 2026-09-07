# STRATEGY_QUARANTINE_POLICY.md

Switching a strategy off. L56, 2026-09-06.

---

## The gap this closed

`app/main.py::_strategy_state` returned:

```python
known = strategy_id in registry.keys()
return StrategyState(exists=known, enabled=known, ...)
```

**`enabled` and `exists` were the same question.** A strategy the platform knew
about was always enabled, so the pipeline's own `strategy_disabled` gate — which
has been in stage 3 since L20 and checks `enabled` and `paused` — **could never
fire for any registered strategy.**

`strategies.is_active` has existed since L05. Nothing read it.

This is the pattern this project keeps finding: a control built, a seat left
unfilled, and a check that reads as protection while being unable to refuse.

## What quarantine is

`strategies.is_active = false`.

**Deliberately the existing column, not a new one.** "No new entries from this
strategy" is exactly what the flag means, and a second quarantine flag beside it
would be a second answer to one question — the condition that makes two
partially-correct states possible.

## What it does

| | |
|---|---|
| New signals from the strategy | **Refused** at pipeline stage 3, `strategy_disabled` |
| The signal | **Consumed**, not parked — re-offering every pass would mean a queue of stale signals firing the moment it is re-enabled |
| Open positions | **Untouched** |
| Position management | **Continues** — Position Manager, RiskEngine, configured exit policies |
| Historical data | **Retained** |

**A strategy being switched off is not a reason to sell at a price nobody
chose.** L56 step 14 says this and it is the right rule: the decision to stop
opening is separate from the decision to close, and conflating them turns an
operator's caution into a forced exit at whatever the market is doing.

## Failure direction

**An unreadable flag leaves the strategy DISABLED.**

If the database cannot be read, the resolver returns `enabled=False` with a
reason saying so. A database blip must not silently re-enable a strategy
somebody switched off, and "I could not check" is not "it is permitted".

A registered strategy with no `strategies` row is treated as enabled: nobody has
ever switched it off, and the registry remains the authority for existence —
the reading L05 gave it.

## Distinctions kept apart

Three refusals that would be easy to blur, and are not:

| Condition | Outcome | Meaning |
|---|---|---|
| Not in the registry | `strategy_unknown` | The platform cannot attribute a trade to it |
| `is_active = false` | `strategy_disabled` | Somebody switched it off |
| Flag unreadable | `strategy_disabled` | We could not check, so we refuse |

They are different faults with different fixes, and the reason string says
which.

## What was NOT built

L56 asks for `HEALTHY / WATCH / DEGRADED / RESTRICTED / QUARANTINED` as a
graded state machine, driven by a strategy health score.

**The grades are not implemented, and the reason is that the inputs do not
exist.** Health would be scored from performance, drawdown, stability,
execution quality, regime fit and correlation contribution. On this platform:

* correlation is unavailable — `market_bars` covers one instrument
* **there is no regime engine at all** — L56 step 5 says to reuse the existing
  one and there is no existing one
* one strategy has ever produced a signal on the automated path, and it
  produced its first order today
* `CLAUDE.md` records that neither traded strategy separates from a coin flip

A five-state machine whose transitions are computed from those inputs would be
grading noise. The binary control is what the evidence supports, and it is the
one that matters: **stop new entries, keep managing what is open.**

The graded states become worth building when there is a regime engine, a market
data feed, and more than one strategy with a measurable record. That order is
recorded in `DYNAMIC_RISK_BUDGETING.md`.

## Regression

`tests/test_signal_routing.py`:

* a switched-off strategy is refused, and the reason says open positions are
  unaffected
* an unknown strategy keeps its own distinct refusal
* an unreadable flag leaves the strategy disabled
