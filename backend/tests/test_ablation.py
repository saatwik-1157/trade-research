"""Whether each component actually adds anything. L75 §33/§52, L76 §52, L77 §44/§70.

The tests that matter here are the ones that stop a component being credited.
An ablation harness that reports "removing X hurt" whenever the number happens
to fall is worse than no harness, because it manufactures a justification for
every component anybody has already built.

`ablate` is a pure function over two evaluated arms, so all of this runs
without a terminal, a database or a market.
"""

from __future__ import annotations

import pytest
from app.validation import ablation
from app.validation.ablation import Arm

# A date-clustered t needs several symbols on several dates before it means
# anything, so the rows below span both. `entry_time` is epoch seconds.
DAY = 86_400


#: Positive with real dispersion. A CONSTANT net gives zero variance, for which
#: the toolkit correctly returns no t at all -- so a fixture of identical trades
#: would test the refusal path rather than the one intended.
WINNING = (1.2, 0.8, 1.1, 0.9, 1.0)


def _rows(
    n: int,
    nets: tuple[float, ...],
    *,
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "USDJPY"),
):
    """`n` trades cycling through `nets`, spread across symbols and days."""
    return [
        {
            "symbol": symbols[i % len(symbols)],
            "net": nets[i % len(nets)],
            "entry_time": 1_700_000_000 + (i // len(symbols)) * DAY,
        }
        for i in range(n)
    ]


def _arm(
    name: str,
    removed: str | None,
    *,
    trades: int,
    expectancy: float,
    nets: tuple[float, ...] = WINNING,
):
    return Arm(
        name=name,
        removed=removed,
        result={
            "trades": trades,
            "expectancy": expectancy,
            "rows": _rows(trades, nets),
        },
    )


def _strong_baseline(expectancy: float = 1.0) -> Arm:
    """A baseline that genuinely clears its own null.

    Positive with dispersion, deliberately. Identical trades give zero variance
    and the toolkit returns no t at all for that -- so the obvious fixture, 90
    trades of exactly 1.0, tests the refusal path rather than the gate, and did
    when this file was first written.
    """
    return _arm("full", None, trades=90, expectancy=expectancy)


# ==================================================== the gate that matters


def test_no_component_is_credited_inside_a_system_that_does_not_work() -> None:
    """The guard this module hangs on.

    If the full system does not beat its own null, "removing X makes it worse"
    is a difference between two noise estimates. Its sign is a coin flip, and
    reporting it as a contribution would manufacture a justification for
    whatever happens to be installed.

    This repository has the receipts: a 36-cell bracket sweep in which the
    RANDOM rule scored 1.76 in sample against the best real candidate's 0.83.
    """
    # Alternating signs: no consistent edge, so no clustered significance.
    noisy = Arm(
        name="full",
        removed=None,
        result={
            "trades": 90,
            "expectancy": 1.0,
            "rows": [
                {
                    "symbol": "EURUSD",
                    "net": 1.0 if i % 2 else -1.0,
                    "entry_time": 1_700_000_000 + i * DAY,
                }
                for i in range(90)
            ],
        },
    )
    report = ablation.ablate(
        noisy,
        [_arm("no-seat", "ai_seat", trades=90, expectancy=0.1)],
    )

    assert report.baseline_established is False
    assert report.contributions[0].verdict == "BASELINE_NOT_ESTABLISHED"
    assert report.demonstrated == ()
    # The delta is still reported. It is the CONCLUSION that is withheld.
    assert report.contributions[0].delta == pytest.approx(0.9)


def test_a_component_can_be_credited_once_the_baseline_is_established() -> None:
    report = ablation.ablate(
        _strong_baseline(1.0),
        [_arm("no-seat", "ai_seat", trades=90, expectancy=0.5)],
    )
    assert report.baseline_established is True
    assert report.contributions[0].verdict == "VALUE_DEMONSTRATED"
    assert report.demonstrated == ("ai_seat",)


# ====================================== not being credited is the default


def test_a_component_that_changes_nothing_is_not_credited() -> None:
    """L75 §33 states the rule: if removing it does not reduce out-of-sample
    performance, it has no demonstrated incremental edge. That is a result, not
    a failure."""
    report = ablation.ablate(
        _strong_baseline(1.0),
        [_arm("no-regime", "regime_model", trades=90, expectancy=1.0)],
    )
    assert report.contributions[0].verdict == "NO_INCREMENTAL_VALUE_DEMONSTRATED"
    assert report.contributions[0].delta == pytest.approx(0.0)


def test_a_component_whose_removal_helps_is_not_credited_either() -> None:
    """A negative contribution is not a positive one with a sign flipped."""
    report = ablation.ablate(
        _strong_baseline(1.0),
        [_arm("no-anomaly", "anomaly", trades=90, expectancy=1.6)],
    )
    assert report.contributions[0].verdict == "NO_INCREMENTAL_VALUE_DEMONSTRATED"
    delta = report.contributions[0].delta
    assert delta is not None and delta < 0


def test_a_difference_below_the_materiality_threshold_is_not_a_finding() -> None:
    """The threshold is deliberately coarse. A finer one would imply a
    precision these sample sizes do not support."""
    report = ablation.ablate(
        _strong_baseline(1.0),
        [_arm("no-seat", "ai_seat", trades=90, expectancy=0.95)],  # 5%, under 10%
    )
    assert report.contributions[0].verdict == "NO_INCREMENTAL_VALUE_DEMONSTRATED"


# =============================================================== samples


def test_a_thin_arm_is_insufficient_rather_than_negative() -> None:
    """Too few trades is a statement about the measurement, not about the
    component -- the same distinction the review layer draws between UNKNOWN
    and POOR."""
    report = ablation.ablate(
        _strong_baseline(1.0),
        [_arm("no-seat", "ai_seat", trades=5, expectancy=0.1)],
    )
    assert report.contributions[0].verdict == "INSUFFICIENT_SAMPLE"


def test_a_thin_baseline_concludes_nothing_about_anything() -> None:
    report = ablation.ablate(
        _arm("full", None, trades=4, expectancy=1.0),
        [_arm("no-seat", "ai_seat", trades=90, expectancy=0.1)],
    )
    assert report.baseline_established is False
    assert "below the 30" in report.baseline_detail
    assert report.demonstrated == ()


# ================================================= N components, N hypotheses


def test_removing_eight_components_is_eight_hypotheses() -> None:
    """Picking the largest of eight deltas and calling it valuable is the
    selection error `research.selection` exists to catch."""
    variants = [_arm(f"no-{i}", f"component_{i}", trades=90, expectancy=0.5) for i in range(8)]
    report = ablation.ablate(_strong_baseline(1.0), variants)

    assert report.family is not None
    assert report.family.tested == 8
    assert report.family.cleared == 8
    # The count expected to look good by chance alone is carried beside it.
    assert report.family.expected > 0


def test_the_family_assessment_is_present_even_when_nothing_cleared() -> None:
    variants = [_arm(f"no-{i}", f"component_{i}", trades=90, expectancy=1.0) for i in range(5)]
    report = ablation.ablate(_strong_baseline(1.0), variants)
    assert report.family is not None
    assert report.family.cleared == 0


# ============================================================ what it refuses


def test_a_variant_that_removed_nothing_is_refused() -> None:
    """A second baseline is not an ablation, and silently reporting a zero for
    it would put a meaningless row in the table."""
    with pytest.raises(ValueError, match="names no removed component"):
        ablation.ablate(_strong_baseline(), [_arm("also-full", None, trades=90, expectancy=1.0)])


def test_a_metric_neither_arm_reported_is_not_measured() -> None:
    report = ablation.ablate(
        _strong_baseline(),
        [_arm("no-seat", "ai_seat", trades=90, expectancy=0.5)],
        metric="sharpe",
    )
    assert report.contributions[0].verdict == "NOT_MEASURED"
    assert report.contributions[0].delta is None


def test_the_report_serialises_without_losing_the_caveat() -> None:
    report = ablation.ablate(
        _strong_baseline(), [_arm("no-seat", "ai_seat", trades=90, expectancy=0.5)]
    )
    out = report.as_dict()
    assert out["baseline"]["established"] is True
    assert "N hypotheses" in out["note"]
    assert out["demonstrated"] == ["ai_seat"]


def test_it_promotes_nothing() -> None:
    """L77 §38: propose, validate, review, approve, adopt. This module is the
    measurement and it must not be the decision -- a function cannot give
    itself the champion/challenger review."""
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "validation" / "ablation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("promote", "adopt", "deploy", "commit", "save", "add", "order_send"):
        assert forbidden not in called, f"ablation calls {forbidden}"
