# LOSS_ROOT_CAUSE_REPORT.md

476 closed trades, 2026-09-04 to 2026-09-10, read from the venue via
`tools/mt5_account.py` on account `5055473926 @ MetaQuotes-Demo` **[DEMO]**.
Not from the platform's own tables — the venue's record is the only one that is
not self-referential.

**Verdict: `BAD STRATEGY`, not `BAD SOFTWARE`.** The system did what it was
configured to do, and the configuration has negative expectancy by arithmetic.

---

## 1. The measured record

```
net profit           -10.51 USD        on a 100,000 USD demo deposit  (-0.0105%)
win rate             80.5%   (383W / 87L, 6 flat)
profit factor        0.964
expectancy / trade   -0.02 USD
average win / loss   0.73 / -3.35
payoff ratio         0.218
largest win / loss   4.96 / -5.24
max consecutive L    7
max drawdown         -65.50 USD        (-0.065%)
median hold          0.17 h            (~10 minutes)
```

Per symbol: EURUSD +15.49, AUDUSD +5.72, GBPUSD +1.16, NZDUSD −2.14,
USDJPY −6.07, USDCAD −9.99, USDCHF −14.51, AUDCAD −0.17.

The AUDCAD row is one hand-opened trade, not the tool's — the magic census
documented in `CLAUDE.md` for 2026-09-07.

---

## 2. The arithmetic, which is the whole answer

An 80.5% win rate sounds like a working system and is the reason this needed
measuring rather than assuming. It is not one, because the wins are small and
the losses are not.

```
payoff ratio                 = 0.73 / 3.35        = 0.218
break-even win rate          = 1 / (1 + 0.218)    = 82.1%
actual win rate                                   = 80.5%
shortfall                                         = 1.6 points
```

Put in trades: of the 470 decided trades, **386 wins were needed to break even
and 383 arrived.** Three trades in four hundred and seventy separate this
configuration from flat. That is the entire loss, and it is why the result is
−10.51 rather than a catastrophe: the geometry is very slightly the wrong side
of even, not badly wrong.

This is the `--min-profit 0.50` harvest geometry doing exactly what
`CLAUDE.md` says it does: *"Closing at the first sign of profit books winners
and holds losers."* A +0.50 harvest against a 1.5×ATR stop is a ~1:6 reward
geometry, and a ~1:6 geometry needs ~82% accuracy to survive. Random entry
does not supply 82%. It supplies about 80.5%, which is what the geometry alone
produces, minus the spread.

---

## 3. Classification, per the §4 taxonomy

Every one of the 476 trades falls in the same place, so a per-trade table would
be 476 identical rows. The causes actually present:

| Cause | Present | Evidence |
|---|---|---|
| `OVERTRADING` | **yes, primary** | 476 trades in 6 days; 20-second passes × 7 pairs; median hold 10 min |
| `SPREAD` | **yes, primary** | expectancy −0.02/trade ≈ the per-trade spread cost; the `random` rule is documented at t = −3.60 for exactly this |
| `STRATEGY_ERROR` | **yes, by configuration** | the sessions ran `--rule random`. See §4 |
| `NORMAL LOSING TRADE` | yes | 87 losses at −3.35 avg are the stop working as designed |
| `SIGNAL_ERROR` | no | there is no signal; `random` has no thesis to be wrong about |
| `AI_ERROR` / `MODEL_ERROR` | **no** | no model is consulted anywhere in this path |
| `RISK_ENGINE_ERROR` | no | the RiskEngine was never in this path |
| `POSITION_SIZE_ERROR` | no | `lot_for_risk()` off the stop distance; scale-invariant |
| `DUPLICATE_SIGNAL` / `DUPLICATE_ORDER` | no | none observed in the logs |
| `WRONG_SYMBOL` / `WRONG_DIRECTION` | no | none observed |
| `STOP_LOSS_ERROR` | **1 of 27 historically** | the NZDUSD bracket inverted at fill, already found, already fixed by `bracket_is_sane()`, flagged `bracket_repaired` |
| `EXECUTION_ERROR` | no | 4 `order_send` sites, retcode DONE |
| `BROKER` / `MT5` | see below | the disconnect is a stability defect, not a loss cause |

---

## 4. The configuration is the finding

Every recent session log records its own command line. All of them read:

```
take_profit.py --rule random --symbols EURUSD,GBPUSD,USDJPY,USDCAD,AUDUSD,USDCHF,NZDUSD \
  --risk-usd 5 --sl-atr 1.5 --tp-atr 1.5 --min-profit 0.50 --max-positions 7 \
  --max-daily-loss 200 --interval 20 --minutes N --flat-by 06:00 --relax-over 45 --live
```

`--rule random` is not a bug and not a fallback. It is a deliberate research
setting whose purpose is to measure **cost drag with the edge removed**, and
`CLAUDE.md` records what it measures: the `random` rule loses at t = −3.60,
*"cost drag is the one effect here large enough to measure."*

The system has therefore been performing its designed experiment, nightly, and
reporting the designed result. What went wrong is not the result — it is that a
measurement configuration was left running as though it were a trading
configuration.

The separation the brief asks for:

- **BAD SOFTWARE** — the disconnect handling (§4 of the audit) and the missed
  flush (§2 of the audit). Real, fixable, and they cost *variance*, not
  expectancy. Had every session flushed on time, the net would still be
  negative; it would be less ragged.
- **BAD STRATEGY** — `--rule random` at a 1:6 harvest geometry, 476 trades in
  six days. This is the expectancy, and it is negative by arithmetic.
- **NORMAL LOSING TRADE** — the 87 stopped-out trades. The stop worked.
- **EXCESSIVE RISK** — **not present.** Max drawdown −65.50 on 100,000, risk
  5.00 USD per trade, 7 positions max, no size escalation. Loss control held.

---

## 5. What would and would not change the result

**Would not:** fixing the crashes, fixing the disconnect handler, a better
model, more indicators, more strategies, parameter tuning, lower spread hours.
`CLAUDE.md` measured the hour filter (−6.64 → −7.40 expectancy, 18 of 41
improved — a coin flip) and financing (41 of 41 candidates worse, 0 verdicts
changed). *"Nothing here was ever one cost adjustment away from working."*

**Would:** trading less often, and requiring a validated signal before entry.
Frequency is the one lever this repository has demonstrated a sign for, and it
points down. That is the same conclusion the TradingView-first architecture
reaches from the other direction — *no valid signal, no trade* is, arithmetically,
a frequency reduction with a thesis attached.

---

## 6. Honest limits of this report

- Six days and 476 trades is a large sample of a **short window**; it measures
  this configuration in this regime, not in all regimes.
- `-10.51` on a demo deposit is not evidence about live behaviour, where
  slippage and fill quality differ.
- The classification above is per-configuration, not per-trade. A per-trade
  causal table as the brief describes requires the trade journal to carry
  signal → strategy → risk → size → fill lineage; **Path A writes no such
  lineage**, because it has no signal, no strategy record and no risk step to
  write down. Producing that table is a reason to move to Path B, not a
  reporting task that can be done against the data that exists.
