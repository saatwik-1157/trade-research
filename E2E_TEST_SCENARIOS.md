# E2E_TEST_SCENARIOS.md

The scenarios L40 exercises, and the test that carries each. All in
`backend/tests/test_integration.py` unless stated otherwise.

---

## S1 — The happy path (step 42)

`test_a_webhook_becomes_an_order_a_position_and_a_journal_row`

```
TradingView payload
  -> POST /v1/webhooks/tradingview      secret checked, freshness checked,
                                        ticker resolved, idempotency key built
  -> signals row                        one, with a signal_key
  -> ExecutionPipeline.process          strategy state, safe mode, staleness,
                                        AI seat, RiskEngine, PositionSizing
  -> OrderManagerRegistry               intent_id, order record
  -> FakeBroker (paper)                 one order placed
```

**What is asserted is the chain, not the outcome.** Any subsystem's own suite
would pass with the foreign keys unset; these are the links a refactor silently
drops:

| Link | Assertion |
|---|---|
| order → sizing | `result.order.sizing_snapshot["execution_id"] == result.execution_id` |
| order → risk | `result.order.risk_decision_id` is set |
| order → idempotency | `result.order.client_order_id` is set |
| order → signal | `result.order.signal_id == row.id` |
| venue | `venue._orders_placed == 1` |

**Not asserted: a position or a journal row.** `FakeBroker` acknowledges an
order and does not fill it, so a position row here would mean the platform had
invented one. Journal consistency against a real fill is `test_trade_journal.py`.

A companion, `test_the_quantity_the_venue_received_is_the_one_sizing_computed`,
checks the number survived the handoff: a multiple of the venue's `volume_step`
and inside its min/max.

## S2 — Duplicate delivery (steps 13, 45, 49)

| Test | Level | Claim |
|---|---|---|
| `test_the_same_alert_twice_produces_one_signal` | gateway | one payload posted twice → one signal |
| `test_a_burst_of_identical_alerts_produces_one_signal` | gateway | ten deliveries → one signal, **no 5xx** |
| `test_the_same_signal_twice_produces_one_order` | pipeline | the in-process `seen` set holds |
| `test_a_concurrent_duplicate_produces_one_order` | pipeline | **genuinely raced** via `asyncio.gather`; exactly one order |
| `test_the_duplicate_race_is_answered_rather_than_raised` | source | both unique keys have a race handler |

The burst test is sequential and the reason is a harness limit, not a choice —
see `KNOWN_TEST_LIMITATIONS.md` §2. The pipeline-level race is genuinely
concurrent because it touches no database session.

**Both L40 bugs live here.** See `INTEGRATION_TEST_REPORT.md`.

## S3 — Invalid input (step 14)

`test_an_invalid_alert_creates_no_signal_and_no_order`, parameterised over
seven cases, plus `test_malformed_json_is_refused`:

invalid secret · missing secret · missing ticker · unknown action · missing
timestamp · unparseable timestamp · stale timestamp · malformed JSON

Each asserts three things, and the third is the point: the response is ≥400,
**no signal row exists**, and **no order row exists** — so the refusal happened
before anything downstream could run.

## S4 — Rejection at the right layer (step 43)

Every one asserts the venue received nothing, not merely that the outcome was a
refusal. A test that checked only the verdict would pass on an implementation
that vetoed the signal and sent the order anyway.

| Test | Rejecting layer |
|---|---|
| `test_a_risk_veto_stops_every_later_stage` | RiskEngine (`max_open_positions=0`) |
| `test_an_ai_rejection_stops_the_pass_before_the_venue` | AI seat |
| `test_an_approving_ai_cannot_override_a_risk_veto` | **both** — risk wins |
| `test_a_sizing_refusal_stops_the_pass_before_the_venue` | PositionSizing (below venue minimum; refused, never rounded up) |
| `test_a_disconnected_venue_stops_new_orders` | BrokerAdapter |
| `test_no_write_route_acts_for_an_unauthenticated_caller` | auth, over the whole write surface |

## S5 — The unknown order (step 24, mandatory)

`test_an_unknown_venue_answer_is_never_retried`

The venue's answer is lost (`venue.unknown_next = True`). The platform cannot
know whether an order exists. Asserted: the outcome is not `filled`, and a
**second pass sends nothing further** — `venue._orders_placed` is unchanged.

`created_an_order` is `True` for this state, deliberately, and the test says so:
the question that function answers is "may something exist at the venue?", and
the conservative answer for anything unresolved is yes.

`test_an_unresolved_order_latches_safe_mode_at_startup` carries it into
recovery: an `unknown` order in the database, `run_startup` over it,
`SafeModeReason.unknown_order_state` latched — and the order left byte-for-byte
unchanged, because recovery reports and does not repair.

## S6 — Safe mode blocks trading (steps 40, 58)

`test_safe_mode_refuses_the_pipeline_before_risk_is_consulted` — gate zero
refuses with `Outcome.safe_mode` and nothing reaches the venue.

In `test_recovery.py`: `test_releasing_safe_mode_without_re_authentication_is_refused`
(L39's gate) and `test_entering_safe_mode_needs_no_re_authentication` (the
asymmetry — entering is cautious and free, leaving costs the password).

## S7 — Paper/live isolation (step 51)

| Test | Claim |
|---|---|
| `test_live_trading_is_off_by_default_and_every_gate_is_shut` | `paper`, `live_trading=False`, all 11 `LIVE_GATES` False |
| `test_paper_mode_cannot_reach_a_live_adapter` | a signal claiming `live` is refused by a paper registry; the venue gets nothing |
| `test_no_registered_broker_is_a_real_broker` | no registered adapter names itself live |

## S8 — Failure isolation (step 44)

| Test | Failure | Required behaviour |
|---|---|---|
| `test_a_failing_notification_does_not_fail_a_trade` | the event bus raises on every publish | the order still goes through |
| `test_the_pipeline_returns_a_decision_rather_than_raising` | the strategy service raises | a recorded outcome, no exception, no order |

## S9 — Data consistency (step 47)

`test_the_records_left_behind_agree_with_each_other` — after one run, every
table is asked what it thinks happened: one signal, one venue order, zero
trades, zero positions. The failure this catches is two subsystems that each
work and disagree about how many things happened.

## S10 — Time (step 48)

`test_every_persisted_timestamp_is_utc` — a row written and read back must be
within ten minutes of `datetime.now(UTC)`. Asserted on the value, not on
`tzinfo`, because SQLite returns naive datetimes whatever was stored; a row
written from a local clock on this machine would be hours away.

`test_a_stale_alert_is_measured_against_its_own_timestamp` — freshness is the
alert's own time, never arrival time. Measuring against arrival would make
every re-delivered alert look fresh, which is exactly backwards.

## S11 — Security in a real flow (step 41)

| Test | Claim |
|---|---|
| `test_an_unauthenticated_caller_reaches_no_business_action` | seven read surfaces answer 401/403 |
| `test_no_write_route_acts_for_an_unauthenticated_caller` | **every** `/v1/` write route is called with no session; none answers 2xx |
| `test_a_security_event_reaching_the_bus_carries_no_identifier` | a `system`-channel security alert carries a count and a class, never a subject |

Plus `test_security.py`'s 44, of which the load-bearing ones are the step-up
gate on dangerous admin actions, single-use grants, and the per-user WebSocket
cap.

## S12 — No future leakage (step 50)

`test_no_execution_stage_can_see_a_bar_after_the_signal` — `process` takes
`now` explicitly, reads the clock at most once (the documented default), and
`_process` never reads it at all. Two different "nows" in one decision is how a
signal gets compared against a bar that had not closed when it was generated.
