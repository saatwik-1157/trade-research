"""What the platform may do to itself without asking. **The L61 regression.**

L61 step 39 lists the safety tests by name. They are the first section here,
worded as the brief words them.

The property underneath all of them: **autonomy may take permissions away and
never grant them.** Classification is by what an action DOES, not what it is
called, because a list of banned names is a blocklist one rename away from a
hole.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.portfolio.control import (
    Action,
    ActionClass,
    ActionLedger,
    Effect,
    as_dict,
    classify,
    permitted,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# =============================== 1. L61 step 39, the critical safety tests


def test_increasing_the_maximum_risk_is_forbidden() -> None:
    """L61 step 39, test 1."""
    verdict, reason = classify(
        Action(
            kind="increase_max_portfolio_risk",
            target="max_portfolio_risk",
            effect=Effect.LOOSENS,
            touches_hard_limit=True,
        )
    )
    assert verdict is ActionClass.FORBIDDEN
    assert "never grant them" in reason


def test_increasing_leverage_is_forbidden() -> None:
    """L61 step 39, test 2."""
    verdict, _ = classify(
        Action(
            kind="increase_leverage",
            target="max_leverage",
            effect=Effect.LOOSENS,
            touches_hard_limit=True,
        )
    )
    assert verdict is ActionClass.FORBIDDEN


def test_reducing_allocation_within_bounds_is_bounded_autonomy() -> None:
    """L61 step 39, test 3. The gate permits what it is for."""
    verdict, reason = classify(
        Action(
            kind="reduce_strategy_allocation",
            target="strategy:sma_cross:allocation",
            effect=Effect.TIGHTENS,
            within_bounds=True,
        )
    )
    assert verdict is ActionClass.AUTO_APPLY_BOUNDED
    assert "within an approved range" in reason


def test_ai_recommending_that_risk_protection_be_disabled_is_rejected() -> None:
    """L61 step 39, test 8. It does not matter who proposed it."""
    verdict, _ = classify(
        Action(
            kind="ai_disable_drawdown_protection",
            target="max_drawdown_pct",
            effect=Effect.LOOSENS,
            touches_hard_limit=True,
        )
    )
    assert verdict is ActionClass.FORBIDDEN


def test_an_emergency_halt_may_be_applied_autonomously() -> None:
    """L61 step 39, test 9. A control expensive to engage is one nobody
    engages in an emergency — the same reasoning the safe-mode route already
    gives for requiring no step-up."""
    verdict, reason = classify(
        Action(
            kind="enter_safe_mode",
            target="safe_mode",
            effect=Effect.TIGHTENS,
            touches_hard_limit=True,
        )
    )
    assert verdict is ActionClass.AUTO_APPLY_BOUNDED
    assert "nobody engages in an emergency" in reason


# ============================ 2. forbidden by classification, not by name


@pytest.mark.parametrize(
    "kind,target",
    [
        ("enable_live_trading", "live_trading"),
        ("disable_kill_switch", "kill_switch"),
        ("widen_drawdown_tolerance", "max_drawdown_pct"),
        ("a_name_nobody_thought_of", "max_position_size"),
        ("innocuous_sounding_tweak", "max_daily_loss"),
    ],
)
def test_any_loosening_of_a_hard_limit_is_forbidden_whatever_it_is_called(
    kind: str, target: str
) -> None:
    """**The point of classifying by effect.**

    A blocklist of names is one rename, one typo or one new action kind away
    from a hole. These include two deliberately innocuous names, and they are
    refused for what they do.
    """
    verdict, _ = classify(
        Action(kind=kind, target=target, effect=Effect.LOOSENS, touches_hard_limit=True)
    )
    assert verdict is ActionClass.FORBIDDEN


def test_bounds_and_reversibility_cannot_rescue_a_forbidden_action() -> None:
    """There is no `within_bounds` that makes raising a ceiling acceptable, and
    no reviewer path here either. A loop that could be argued into raising its
    own ceiling has no ceiling."""
    verdict, _ = classify(
        Action(
            kind="tiny_reversible_in_bounds_risk_increase",
            target="max_portfolio_risk",
            effect=Effect.LOOSENS,
            touches_hard_limit=True,
            within_bounds=True,
            reversible=True,
        )
    )
    assert verdict is ActionClass.FORBIDDEN


def test_loosening_something_soft_needs_approval_not_a_ban() -> None:
    """Not everything that grants room is forbidden — only hard constraints
    are. Increasing an allocation is a real thing somebody may authorise."""
    verdict, reason = classify(
        Action(
            kind="increase_strategy_allocation",
            target="strategy:sma_cross:allocation",
            effect=Effect.LOOSENS,
            touches_hard_limit=False,
            within_bounds=True,
        )
    )
    assert verdict is ActionClass.REQUIRES_APPROVAL
    assert "has no in-range exception" in reason


def test_only_tightening_is_ever_automatic() -> None:
    """The property every "never automatically increase" rule reduces to."""
    automatic = []
    for effect in Effect:
        for hard in (True, False):
            verdict, _ = classify(
                Action(kind="x", target="t", effect=effect, touches_hard_limit=hard)
            )
            if verdict is ActionClass.AUTO_APPLY_BOUNDED:
                automatic.append(effect)
    assert set(automatic) == {Effect.TIGHTENS}


def test_a_large_or_irreversible_tightening_still_needs_a_person() -> None:
    """Reducing exposure is the safe direction, and a reduction nobody sized is
    still a change nobody sized."""
    big, _ = classify(
        Action(kind="slash", target="alloc", effect=Effect.TIGHTENS, within_bounds=False)
    )
    oneway, _ = classify(
        Action(kind="retire", target="alloc", effect=Effect.TIGHTENS, reversible=False)
    )
    assert big is ActionClass.REQUIRES_APPROVAL
    assert oneway is ActionClass.REQUIRES_APPROVAL


# ================================= 3. a loop that oscillates stops controlling


def _reduce(target: str = "alloc") -> Action:
    return Action(kind="reduce", target=target, effect=Effect.TIGHTENS)


def _restore(target: str = "alloc") -> Action:
    return Action(kind="restore", target=target, effect=Effect.LOOSENS, touches_hard_limit=False)


def test_a_thrashing_target_stops_being_adjusted_automatically() -> None:
    """**The L51 defect in another costume.** The bot supervisor had five good
    gates and none asked *how many times*. Reduce-restore-reduce looks like
    responsiveness and is a loop arguing with itself.
    """
    ledger = ActionLedger()
    for i in range(6):
        action = _reduce() if i % 2 == 0 else _restore()
        ledger.record(action, at=NOW + timedelta(minutes=i))

    verdict, reason = permitted(_reduce(), ledger, now=NOW + timedelta(minutes=7))
    assert verdict is ActionClass.REQUIRES_APPROVAL
    assert "arguing with itself" in reason


def test_a_normal_correction_is_not_thrashing() -> None:
    """Reduce, then restore when the condition clears, is the mechanism
    working. Two direction changes must not trip the guard."""
    ledger = ActionLedger()
    ledger.record(_reduce(), at=NOW)
    ledger.record(_restore(), at=NOW + timedelta(minutes=10))

    verdict, _ = permitted(_reduce(), ledger, now=NOW + timedelta(minutes=20))
    assert verdict is ActionClass.AUTO_APPLY_BOUNDED


def test_oscillation_is_counted_per_target() -> None:
    """One busy strategy must not freeze adjustments to a quiet one."""
    ledger = ActionLedger()
    for i in range(6):
        action = _reduce("noisy") if i % 2 == 0 else _restore("noisy")
        ledger.record(action, at=NOW + timedelta(minutes=i))

    assert ledger.oscillating("noisy", now=NOW + timedelta(minutes=7)) is not None
    assert ledger.oscillating("quiet", now=NOW + timedelta(minutes=7)) is None


def test_the_window_expires() -> None:
    """A loop an hour ago is not a loop now, or the guard would latch forever
    on a system that has since settled."""
    ledger = ActionLedger()
    for i in range(6):
        action = _reduce() if i % 2 == 0 else _restore()
        ledger.record(action, at=NOW + timedelta(minutes=i))

    assert ledger.oscillating("alloc", now=NOW + timedelta(minutes=7)) is not None
    assert ledger.oscillating("alloc", now=NOW + timedelta(hours=3)) is None


def test_thrashing_never_blocks_an_emergency_stop() -> None:
    """**The most important interaction in this file.**

    If instability could suppress the response to instability, the guard would
    disable the thing it exists to protect.
    """
    ledger = ActionLedger()
    for i in range(8):
        action = _reduce("safe_mode") if i % 2 == 0 else _restore("safe_mode")
        ledger.record(action, at=NOW + timedelta(minutes=i))
    assert ledger.oscillating("safe_mode", now=NOW + timedelta(minutes=9)) is not None

    verdict, _ = permitted(
        Action(
            kind="enter_safe_mode",
            target="safe_mode",
            effect=Effect.TIGHTENS,
            touches_hard_limit=True,
        ),
        ledger,
        now=NOW + timedelta(minutes=9),
    )
    assert verdict is ActionClass.AUTO_APPLY_BOUNDED


def test_oscillation_can_never_promote_an_action() -> None:
    """The guard demotes; it must not be a path to more autonomy."""
    ledger = ActionLedger()
    for i in range(8):
        ledger.record(_reduce("x") if i % 2 == 0 else _restore("x"), at=NOW + timedelta(minutes=i))

    forbidden, _ = permitted(
        Action(kind="raise", target="x", effect=Effect.LOOSENS, touches_hard_limit=True),
        ledger,
        now=NOW + timedelta(minutes=9),
    )
    assert forbidden is ActionClass.FORBIDDEN


# ==================================================== 4. it applies nothing


def test_nothing_here_reaches_risk_the_oms_or_a_venue() -> None:
    """A control layer that could apply its own decisions would be the L45 C-2
    defect at the top of the stack: a component assuming another's authority."""
    import ast
    import inspect

    from app.portfolio import control

    tree = ast.parse(inspect.getsource(control))
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
        "execute",
        "commit",
    ):
        assert forbidden not in called, f"the control module calls {forbidden}()"

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert [m for m in imported if m.startswith("app.")] == []


def test_the_record_says_it_applied_nothing() -> None:
    action = _reduce()
    verdict, reason = classify(action)
    record = as_dict(action, verdict, reason)
    assert "not an application" in str(record["authority"])
    assert "RiskEngine remains the final veto" in str(record["authority"])
