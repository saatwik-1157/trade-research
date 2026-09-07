# STRATEGY_SPECIFICATION.md

The canonical internal form of a TradingView strategy, and the exact Pine
subset that can reach it. Brief §4 and §6.

---

## 1. Two contracts, not one

The brief's §6 shape and the platform's existing `StrategyDefinition` (L13) are
**different artifacts and both are kept**:

| | `TradingViewSpec` | `StrategyDefinition` |
|---|---|---|
| Written by | the Pine parser (A1) | the compiler (A4), or the visual builder (L13) |
| Faithful to | the *source* — including what cannot be run | the *platform* — only what the evaluator implements |
| Holds | warnings, unsupported features, declared sizing, sessions, alerts | indicators, conditions, entry/exit rules |
| Executed by | nothing, ever | `app.strategies.built.BuiltStrategy` |
| Lives in | `tv_strategy_sources.spec` (A2) | `strategy_versions.config` (exists) |

The spec is the record of what the user gave us. The definition is the subset
the platform will actually run. Collapsing them would mean either the spec
silently dropped the parts we cannot run — so an operator would believe a
session filter was in force when it was not — or the definition carried fields
the evaluator ignores. Both are the same failure: a strategy running that
nobody wrote.

`StrategyDefinition` is **not modified** by this work. It already round-trips
(`to_payload`), already refuses unknown fields, and already refuses
`PRICE > RSI` on units.

## 2. The `TradingViewSpec` shape

```json
{
  "source": "TRADINGVIEW",
  "source_kind": "pine_script",
  "strategy_id": "donchian-breakout",
  "name": "Donchian Breakout",
  "version": 1,
  "pine_version": 5,
  "hash": "sha256:...",
  "symbol": null,
  "timeframe": null,
  "indicators": [
    {"pine": "ta.rsi", "key": "RSI", "params": {"period": 14},
     "unit": "oscillator", "from_input": "rsiLen"}
  ],
  "inputs": [
    {"name": "rsiLen", "pine_type": "input.int", "default": 14,
     "minval": 2, "maxval": 100}
  ],
  "expressions": [
    {"type": "condition", "left": {"kind": "indicator", "ref": "RSI"},
     "comparison": "LESS_THAN", "right": {"kind": "constant", "value": 30}}
  ],
  "variables": [
    {"name": "isOversold", "expression_ref": 0, "single_assignment": true}
  ],
  "entry_rules": [
    {"side": "long", "when_ref": 0, "pine_call": "strategy.entry"}
  ],
  "exit_rules": [
    {"side": "long", "when_ref": 1, "pine_call": "strategy.close"}
  ],
  "risk_rules": {
    "stop_loss": {"kind": "atr_multiple", "multiple": 1.5,
                  "pine": "strategy.exit(stop=)"},
    "take_profit": {"kind": "atr_multiple", "multiple": 1.5},
    "trailing_stop": null
  },
  "position_sizing": {
    "declared": {"qty_type": "percent_of_equity", "qty_value": 10},
    "obeyed": false,
    "note": "recorded, never obeyed; the platform recomputes through Risk then Sizing"
  },
  "filters": [],
  "sessions": [{"pine": "time(timeframe.period, ...)", "supported": false}],
  "alerts": [{"pine": "alertcondition", "message": "..."}],
  "execution_rules": {
    "process_orders_on_close": false,
    "calc_on_every_tick": false,
    "pyramiding": 0
  },
  "dependencies": [],
  "warnings": ["symbol and timeframe are not stated in the source"],
  "unsupported_features": [
    {"pine": "request.security", "line": 42,
     "reason": "another symbol or timeframe; repaints and can leak future bars",
     "effect": "compilation refused"}
  ],
  "compilable": false
}
```

Field rules that carry weight:

- **`hash`** is over the normalized source text, not over the spec, so a
  whitespace-only edit does not mint a new version and a logic edit always
  does. It is the identity brief §12 asks for.
- **`compilable`** is false whenever `unsupported_features` is non-empty, or
  `symbol`/`timeframe` is null. It is computed, never set by a caller.
- **`when_ref`** indexes the `expressions` array, so a condition shared by an
  entry and an exit is stored once and cannot drift between them.
- **`warnings`** never blocks; **`unsupported_features`** always does. A
  strategy is not compiled "with warnings suppressed" — the two lists exist so
  that distinction lives in the data rather than in a log line.
- Nothing is inferred. A field the source does not state is `null` and named
  in `warnings` — which is `data_gaps` from the research toolkit under another
  name (CLAUDE.md: report the gap, never fill it from memory).

## 3. The supported Pine subset, declared

The evaluator this compiles to (`app/strategies/built.py`) implements exactly
4 indicators, 5 price fields, 7 comparisons, AND/OR/NOT, depth ≤ 6, ≤ 40
conditions, ≤ 10 rules per side and 5 rule actions. The subset is therefore
small, and saying so precisely is what makes refusal possible.

**Supported**

| Pine | Maps to | Note |
|---|---|---|
| `//@version=4,5,6` | `pine_version` | v3 and earlier refused |
| `strategy("name", ...)` | `name`, `execution_rules` | |
| `input.int/float/bool` | `inputs`, then indicator `params` | Pine's `minval`/`maxval` carried into `ParameterSpec` validation |
| `ta.sma / ta.ema / ta.rsi / ta.atr` | `SMA / EMA / RSI / ATR` | the whole catalogue |
| `close open high low volume` | `PRICE_FIELDS` | |
| `> < >= <= ==` | the five value comparisons | |
| `ta.crossover / ta.crossunder` | `CROSSES_ABOVE / CROSSES_BELOW` | reads T and T−1 only |
| `and or not` | `AND / OR / NOT` groups | |
| `x = <pure expression>` | `variables`, inlined at compile | single assignment only |
| `strategy.entry(..., strategy.long/short)` | `ENTRY_LONG / ENTRY_SHORT` | one per side |
| `strategy.close / strategy.close_all` | `EXIT_LONG / EXIT_SHORT / CLOSE` | |
| `strategy.exit(stop=, limit=)` | `risk_rules`, **not** the evaluator | brackets belong to the position manager (L21) and the paper engine's bracket check |
| `plot / plotshape / bgcolor` | ignored, recorded | cosmetic; cannot change behaviour |
| `alertcondition / alert()` | `alerts` | recorded; never wired to execution |

**Unsupported — recorded, and compilation refused**

| Pine | Why it cannot be silently dropped |
|---|---|
| `request.security(...)` | another symbol or timeframe. Repaints, and is the classic way a backtest reads a bar that had not closed. L08 owns multi-timeframe data and the evaluator has no second series |
| `for` / `while`, arrays, matrices, maps | unbounded computation inside a definition that is walked, not compiled |
| user-defined functions, `var` / `varip` | mutable state across bars; the evaluator is stateless by construction |
| `barstate.*`, `timenow`, `time(...)` sessions | intrabar and wall-clock semantics. `SignalTiming.bar_close` is declared rather than assumed, and a session filter has no operand kind yet |
| any other `ta.*` | offering MACD or Bollinger from `tools/indicators.py` would mean a second implementation over a different data shape. L13 refused this deliberately and A4 does not reopen it |
| `pyramiding > 0`, `strategy.order` scaling | multiple entries per direction; the paper position model is one position per symbol per account |
| `calc_on_every_tick=true`, `process_orders_on_close=true` | changes when a signal is considered generated. A backtest that mixed these silently describes something the live loop does not do |
| `strategy.percent_of_equity` as an *instruction* | recorded in `position_sizing.declared` with `obeyed: false`. Sizing is Risk → `app.sizing`, which refuses on missing tick data rather than guessing a lot |
| libraries (`import user/lib/1`) | third-party source that is not in this repository |

An unsupported feature is never approximated. Brief §4 is explicit — flag it,
do not invent it — and this project has already paid twice for arithmetic that
merely looked like a result (the metals points error, then the indices repeat).

## 4. Versioning

Brief §12 says never overwrite. The existing tables already do this correctly
and are reused as they stand.

- A source whose `hash` matches an accepted row is the **same** strategy: the
  drop is recorded and no version is created.
- A changed hash creates `strategy_versions.version = n+1` with
  `code_ref = "app.strategies.built:BuiltStrategy"` and the compiled definition
  in `config` — exactly what the L13 builder already writes.
- `status` starts `draft` and becomes `validated` only through
  STRATEGY_VALIDATION.md's gates. The lifecycle state (brief §32) is tracked in
  a separate additive column, because `draft/validated/retired` cannot express
  BACKTESTED or PAPER without lying about one of them.
- The tier is `research_only` on every version, always, because
  `BuiltStrategy.metadata()` says so and it is telling the truth: a compiled
  strategy has been measured by nobody.
