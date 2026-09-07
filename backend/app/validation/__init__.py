"""The AI validation engine: is this candidate valid enough to be considered?

Level 26. The question this package answers is narrow and the narrowness is the
design: **not "is this model good", not "should we deploy it", but "does the
evidence support a claim about it at all, and if so what claim".**

Six modules:

  * `config`   — the thresholds (every one configurable, §25) and the verdict
                 rule, which is precedence and not arithmetic.
  * `statistics` — the permutation null, AUC, the bootstrap, and the toolkit's
                 own date/symbol clustering.
  * `economics` — the model's decisions run through
                 `tools/rule_backtest.simulate`, the engine every measured
                 figure in `CLAUDE.md` came from.
  * `checks`   — one function per check, each returning its own severity.
  * `report`   — the findings, the verdict, and what the verdict means.
  * `service`  — the background job, on L14's shape.

**Three things this package cannot do**, each enforced rather than intended:

1. **Trade.** No module imports an order manager, a broker adapter, a risk
   engine, a position sizer or a strategy runner. `tests/test_validation.py`
   parses every module with `ast` and fails on such an import.
2. **Promote.** Nothing writes `model_versions.status`. A PASS means the
   candidate satisfies the validation requirements that were checked; it is
   not a deployment, not an allocation and not an instruction.
3. **Invent a number.** Every figure is measured from the candidate's own
   predictions on the final test segment, or the check reports BLOCKED and says
   what was missing. There is no default profit factor and no hardcoded PASS.
"""

from app.validation.config import (
    MAX_CONCURRENT,
    MAX_QUEUED_PER_USER,
    VALIDATION_ENGINE_VERSION,
    Severity,
    Thresholds,
    ValidationConfig,
    ValidationError,
    ValidationStage,
    Verdict,
    progress_of,
    verdict_from,
)

__all__ = [
    "MAX_CONCURRENT",
    "MAX_QUEUED_PER_USER",
    "VALIDATION_ENGINE_VERSION",
    "Severity",
    "Thresholds",
    "ValidationConfig",
    "ValidationError",
    "ValidationStage",
    "Verdict",
    "progress_of",
    "verdict_from",
]
