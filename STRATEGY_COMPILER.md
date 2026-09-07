# STRATEGY_COMPILER.md

`TradingViewSpec` → `StrategyDefinition`, and what the compiler refuses.
Brief §11.

---

## 1. The flow, and where it stops

```
Pine source
    ↓  A1 parser            (deterministic; no LLM in this path)
TradingViewSpec             (source-faithful, may be non-compilable)
    ↓  A4 compiler
StrategyDefinition payload  (platform subset only)
    ↓  parse_definition()   (L13 — the existing validator, unchanged)
BuiltStrategy               (L13 — the existing evaluator, unchanged)
    ↓  registry / resolver  (A3 — the missing bridge)
StrategyEngine              (L12 — unchanged)
```

The compiler is a **translation between two data shapes**. It emits no source,
imports nothing by name, and produces no callable. That is inherited rather
than re-argued: L13's registry docstring already says nothing executes code
from a name or a payload, and the compiler's output is a dict that
`parse_definition` validates before anything walks it.

**The compiler is the last stage that may refuse for semantic reasons.** After
it, refusals belong to risk and the broker layer.

## 2. The mapping

| Spec element | Definition element | Notes |
|---|---|---|
| `name` | `name` | truncated to 120 chars, the field's limit |
| `symbol` | `symbol` | uppercased; L11 `get_symbol` must resolve it or the compile fails |
| `timeframe` | `timeframe` | must be one of L08's 8 timeframes |
| `indicators[]` | operand `{kind: indicator, ref, params}` | params resolved from `inputs` defaults, then validated by `ParameterSpec` |
| price references | operand `{kind: price, ref}` | |
| numeric literals | operand `{kind: constant, value}` | |
| `expressions[]` conditions | `{type: condition, left, comparison, right}` | |
| `and` / `or` / `not` | `{type: group, logical, children}` | flattened where associativity permits, to stay inside depth 6 |
| `variables[]` | inlined | a single-assignment Pine variable is a named sub-expression; inlining it is behaviour-preserving and keeps the definition self-contained |
| `entry_rules[]` | `entry_rules[] {when, then: ENTRY_LONG\|ENTRY_SHORT}` | |
| `exit_rules[]` | `exit_rules[] {when, then: EXIT_LONG\|EXIT_SHORT\|CLOSE}` | |
| `risk_rules` | **not** in the definition | carried alongside as the bracket configuration the position manager and paper engine consume. A definition has no place for a stop and inventing one would put two exit authorities in the system |
| `position_sizing` | **not** in the definition | recorded with `obeyed: false` |
| `sessions`, `filters` not expressible | refusal | |

Two mappings deserve their reasons stated.

**Brackets leave the definition.** L13's `StrategyDefinition` has entry and exit
*rules* and no stop field, and that is correct: exits already have an owner in
this platform — 7 policies in `app/positions/policies.py`, priority-ordered,
plus the paper engine's `_bracket`. A compiler that wrote the Pine stop into
the strategy would create a second exit authority racing the first. So
`strategy.exit(stop=, limit=)` becomes bracket configuration attached to the
strategy version, not a rule inside it, and the position manager enforces it
the way it enforces every other stop.

**Variables are inlined, not stored.** Pine's `isOversold = ta.rsi(close,14) < 30`
is a name for a condition. The evaluator has no variable table, and adding one
would be a second place a definition could carry meaning. Inlining is exact for
single-assignment pure expressions, which is the only form the parser accepts.

## 3. Behaviour preservation, and the report

Brief §11: the compiler must preserve behaviour, and document any change. Every
compile therefore emits a **preservation report** stored with the version:

```json
{
  "preserved": true,
  "transformations": [
    {"kind": "inlined_variable", "name": "isOversold", "exact": true},
    {"kind": "flattened_group", "from_depth": 7, "to_depth": 5, "exact": true}
  ],
  "behaviour_changes": [],
  "not_carried": [
    {"element": "strategy.exit(stop=1.5*atr)",
     "moved_to": "bracket configuration (position manager)",
     "equivalent": "enforced per bar by the same policy that enforces every stop"}
  ],
  "unverified_claims": [
    "TradingView's Strategy Tester results for this script have not been reproduced"
  ]
}
```

`preserved: false` does not block the compile on its own — a documented,
inexact transformation is allowed to exist — but it does block the lifecycle at
COMPILED, so nothing reaches BACKTESTED with an undocumented change. Three
transformation kinds are known to be exact and are the only ones A4 implements:
variable inlining, associative group flattening, and constant folding of
numeric literals. Anything else is a refusal, not a "best effort".

## 4. What the compiler refuses

1. **`spec.compilable == false`** — an unsupported feature, or a null symbol or
   timeframe. The refusal names the feature and the line.
2. **An indicator outside the catalogue of four.** The message lists the four,
   because L13's `spec_for` already does exactly that and the compiler should
   not say something different.
3. **A comparison between different units.** `_check_comparable` refuses
   `PRICE > RSI`, and this is where a mistranslated Pine expression surfaces:
   if the compiler mapped `ta.rsi(close, 14) > close` faithfully, the unit check
   catches that the *source* was comparing incomparable quantities, and says so
   rather than running it.
4. **A tree deeper than 6 or wider than 40 conditions** after flattening.
5. **More than one entry rule per side**, until the position model supports it.
6. **An unmapped symbol.** `validate_against_platform` already returns this as
   a problem; the compiler surfaces it rather than saving a draft that can never
   run.

Every refusal is recorded on the source row with its reason, so the same file
dropped twice produces the same refusal and no work.

## 5. What the compiler does not do

- It does not register the strategy as runnable — A2 writes the version row,
  and the tier stays `research_only`.
- It does not decide the strategy is any good. Compiling is a translation
  result, not evidence; `BuiltStrategy.metadata().evidence` already says
  "Built, not measured".
- It does not touch `simulate()`, `lot_for_risk`, `bracket_is_sane`,
  `filling_for`, `assert_demo` or `server_day_start` — standing rule 4.
- It does not use an LLM. The AI interpreter (A5) may *propose* a mapping for an
  ambiguous construct, and that proposal is written into the spec as a
  suggestion with provenance; it becomes a definition only by passing this same
  deterministic compiler and its validator. The project's own rule holds
  unchanged: Python computes, the model interprets, and the model never
  introduces a figure.
