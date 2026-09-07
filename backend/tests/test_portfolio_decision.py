"""What the platform may do right now, and which layer decided it. **L60.**

Two properties, and the tests exist to make them hard to break:

* **A lower layer cannot relax a higher one.** L60's own worked example: AI
  says BUY, the RiskEngine says REJECT, the answer is no order.
* **Missing is never safe.** An absent or stale input makes the decision more
  conservative. This platform has already shipped the opposite — L53's
  `open_symbols` defaulted to an empty set, indistinguishable from "nothing is
  open", so the one aggregate risk control enabled by default could not fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.portfolio.decision import (
    SEVERITY,
    DecisionContext,
    Finding,
    Freshness,
    Input,
    Layer,
    Verdict,
    decide,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _context(**over: object) -> DecisionContext:
    return DecisionContext(now=NOW, **over)  # type: ignore[arg-type]


# ============================================ 1. the hierarchy is the property


def test_ai_confidence_cannot_override_a_risk_restriction() -> None:
    """**L60's worked example.** AI says BUY, risk says REJECT, answer is no."""
    ctx = _context()
    ctx.add_finding(Finding(Layer.SIGNAL, Verdict.ALLOW, "the model is confident this is a buy"))
    ctx.add_finding(Finding(Layer.RISK, Verdict.REJECT, "portfolio drawdown limit breached"))

    decision = decide(ctx)

    assert decision.verdict is Verdict.REJECT
    assert decision.layer is Layer.RISK
    assert decision.allows_new_risk is False
    assert "drawdown" in decision.reason


def test_an_optimizer_cannot_relax_a_portfolio_restriction() -> None:
    """The same rule one layer down. An allocator that wants more exposure does
    not get it by being confident."""
    ctx = _context()
    ctx.add_finding(Finding(Layer.OPTIMIZATION, Verdict.ALLOW, "allocating the remaining budget"))
    ctx.add_finding(Finding(Layer.PORTFOLIO, Verdict.RESTRICT, "concentration limit reached"))

    assert decide(ctx).verdict is Verdict.RESTRICT
    assert decide(ctx).layer is Layer.PORTFOLIO


@pytest.mark.parametrize(
    "lower,higher",
    [
        (Layer.PREFERENCE, Layer.OPTIMIZATION),
        (Layer.OPTIMIZATION, Layer.SIGNAL),
        (Layer.SIGNAL, Layer.STRATEGY),
        (Layer.STRATEGY, Layer.PORTFOLIO),
        (Layer.PORTFOLIO, Layer.RISK),
        (Layer.RISK, Layer.HARD_SAFETY),
    ],
)
def test_no_layer_can_be_relaxed_by_the_one_below_it(lower: Layer, higher: Layer) -> None:
    """Every adjacent pair, so a reordering of the enum fails here."""
    assert lower < higher
    ctx = _context()
    ctx.add_finding(Finding(lower, Verdict.ALLOW, "the lower layer is happy"))
    ctx.add_finding(Finding(higher, Verdict.PAUSE, "the higher layer is not"))
    assert decide(ctx).verdict is Verdict.PAUSE


def test_the_most_restrictive_verdict_wins_regardless_of_order() -> None:
    """The findings arrive in whatever order the collectors ran. That must not
    change the answer."""
    findings = [
        Finding(Layer.RISK, Verdict.RESTRICT, "risk"),
        Finding(Layer.HARD_SAFETY, Verdict.REJECT, "safe mode"),
        Finding(Layer.SIGNAL, Verdict.ALLOW, "signal"),
    ]
    forward = _context()
    for f in findings:
        forward.add_finding(f)
    backward = _context()
    for f in reversed(findings):
        backward.add_finding(f)

    assert decide(forward).verdict is decide(backward).verdict is Verdict.REJECT


def test_every_verdict_has_a_severity() -> None:
    """A verdict added without a severity would sort as most permissive and
    silently become an ALLOW. It raises instead — but only if this holds."""
    assert set(SEVERITY) == set(Verdict)


def test_losing_findings_are_kept() -> None:
    """A record showing only the winner hides that risk and the optimizer
    disagreed, which is the fact somebody needs after an incident."""
    ctx = _context()
    ctx.add_finding(Finding(Layer.SIGNAL, Verdict.ALLOW, "model says go"))
    ctx.add_finding(Finding(Layer.RISK, Verdict.REJECT, "risk says no"))

    decision = decide(ctx)
    assert len(decision.findings) == 2
    reasons = {f.reason for f in decision.findings}
    assert "model says go" in reasons


# ================================================== 2. missing is never safe


def test_a_missing_required_input_defers_the_decision() -> None:
    """Not a warning. A warning that does not change the verdict is not what
    "more conservative" means."""
    ctx = _context()
    ctx.add_finding(Finding(Layer.SIGNAL, Verdict.ALLOW, "model says go"))
    ctx.require_fresh("market_data")

    decision = decide(ctx)
    assert decision.verdict is Verdict.DEFER
    assert decision.layer is Layer.HARD_SAFETY
    assert "never read as permission" in decision.reason


def test_a_stale_input_is_not_a_fresh_one() -> None:
    ctx = _context()
    ctx.add_input(Input("market_data", "marketdata", value=1.10, at=NOW - timedelta(hours=1)))
    ctx.require_fresh("market_data")

    decision = decide(ctx)
    assert decision.verdict is Verdict.DEFER
    assert ("market_data", Freshness.STALE) in decision.degraded_inputs


def test_a_present_but_empty_value_is_MISSING_not_fresh() -> None:
    """**The L53 lesson, generalised.** `open_symbols` defaulted to an empty
    frozenset and read as the positive claim "nothing is open".

    Here a None value is MISSING even with a good timestamp: "we asked and got
    nothing" is an absent fact, not a fresh one.
    """
    item = Input("exposure", "portfolio", value=None, at=NOW)
    assert item.freshness(now=NOW) is Freshness.MISSING


def test_a_future_timestamp_is_invalid_not_fresh() -> None:
    """A reading from the future is a clock problem or a fabricated number.
    Neither is evidence, and "very fresh" is the wrong reading."""
    item = Input("regime", "regime-engine", value="TRENDING", at=NOW + timedelta(minutes=10))
    assert item.freshness(now=NOW) is Freshness.INVALID


def test_an_input_with_no_timestamp_is_invalid() -> None:
    """A value nobody dated cannot be shown to be about now."""
    assert Input("x", "y", value=1).freshness(now=NOW) is Freshness.INVALID


def test_fresh_inputs_do_not_restrict() -> None:
    """The gate refuses what is unsafe, not everything."""
    ctx = _context()
    ctx.add_input(Input("market_data", "marketdata", value=1.10, at=NOW))
    ctx.require_fresh("market_data")

    decision = decide(ctx)
    assert decision.verdict is Verdict.ALLOW
    assert decision.degraded_inputs == ()


def test_degraded_inputs_are_reported_even_when_they_do_not_decide() -> None:
    """An input can be stale without being required. It still appears on the
    record, because "we decided while this was stale" is worth knowing."""
    ctx = _context()
    ctx.add_input(Input("regime", "regime-engine", value="RANGING", at=NOW - timedelta(days=1)))

    decision = decide(ctx)
    assert decision.verdict is Verdict.ALLOW
    assert ("regime", Freshness.STALE) in decision.degraded_inputs


# ==================================================== 3. it decides no orders


def test_nothing_here_reaches_risk_the_oms_or_a_venue() -> None:
    """The module's central claim, asserted from the syntax tree.

    A decision layer that could submit an order would be the C-2 defect at a
    higher altitude: a component quietly assuming another's authority.
    """
    import ast
    import inspect

    from app.portfolio import decision as module

    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in (
        "place_order",
        "close_position",
        "cancel_order",
        "submit",
        "approve",
        "set_limits",
        "create",
    ):
        assert forbidden not in called, f"the decision module calls {forbidden}()"

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    reaching = [m for m in imported if m.startswith("app.") and not m.startswith("app.portfolio")]
    assert reaching == [], f"the decision module reaches into the platform: {reaching}"


def test_the_decision_says_it_is_only_a_recommendation() -> None:
    """Read by whatever displays it, so the authority is on the record rather
    than in a document somebody has to find."""
    note = str(decide(_context()).as_dict()["authority"])
    assert "RiskEngine remains the final veto" in note
    assert "OMS owns" in note


def test_an_empty_context_allows_but_only_because_absence_is_a_finding() -> None:
    """With no findings the answer is ALLOW, which is only safe because
    `require_fresh` turns absent evidence into a finding rather than silence.
    If that ever stops being true, this test is where to start."""
    decision = decide(_context())
    assert decision.verdict is Verdict.ALLOW
    assert decision.layer is Layer.PREFERENCE

    ctx = _context()
    ctx.require_fresh("anything_at_all")
    assert decide(ctx).verdict is Verdict.DEFER
