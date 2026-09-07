# EXECUTION_STRESS_POLICY.md

L66 section 26. Latency, rejection, disconnect.

---

## Bounded inputs

`latency_ms` and `slippage_pct`, both bounded.

## Unknown execution state reconciles

Section 26's last two lines, and they are the platform's oldest rule rather
than a new one: `SAFE_TO_RESEND` is `{failed}` and nothing else, because an IPC
timeout after `order_send` looks exactly like a rejection from this side.

`gate()` returns REJECT on `unreconciled_order=True` with the reason *settled by
asking the venue, never by acting on a forecast about it*. A scenario cannot
predict its way past an unknown.

## Never blind retries

There is no retry in this module, and no caller of one. The scenario layer
produces a verdict; the execution path is the OMS's, and
`test_only_the_oms_reaches_a_venue_to_write` (L62) covers the whole codebase.

## Not built

Simulated execution degradation against a real order flow. The platform has 271
orders, all from an imported ledger, and one open position. There is no live
execution stream to degrade.

The existing fault-injection tests cover broker unreachable, MT5 disconnect and
unknown order state as *behaviours*, which is the part that matters and which
already passes.
