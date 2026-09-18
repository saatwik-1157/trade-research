"""What the platform is allowed to do without a person. **L51.**

Two questions, and they are not the same one:

  * **What may run unattended?** A worker tick, a supervisor sweep, a recovery
    step — code nobody asked for at the moment it runs.
  * **What may that code CHANGE?** Trading through the gated path is the
    platform's purpose, so "a worker must not reach a venue" would be both
    false and wrong to assert. What must never happen autonomously is a change
    to *what is permitted*: risk limits, kill switches, safe mode, model
    promotion, live-trading settings.

`test_no_autonomous_path_can_change_what_is_permitted` is the Phase 30 audit,
and it walks the import graph from every `Worker` subclass rather than trusting
that nobody wired one.

The recovery-budget tests live in `tests/test_bots.py` beside the supervisor
they constrain.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


# ============================================================ the import graph


def _module_path(dotted: str) -> Path | None:
    """`app.bots.supervisor` -> the file, if it is ours."""
    if not dotted.startswith("app"):
        return None
    rel = Path(*dotted.split(".")[1:])
    for candidate in (APP / rel.with_suffix(".py"), APP / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(tree: ast.Module, own: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative
                base = own.rsplit(".", node.level)[0] if "." in own else "app"
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if module:
                out.add(module)
                # `from app.risk import service` imports a module, not a name.
                out.update(f"{module}.{a.name}" for a in node.names)
    return out


def _dotted(path: Path) -> str:
    rel = path.relative_to(APP.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _autonomous_entry_points() -> list[Path]:
    """Every module defining a `Worker` subclass — code that runs unattended.

    Found by walking the source rather than by importing, so a worker added in
    a module nobody thought to list is still caught.
    """
    found: list[Path] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {
                b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases
            }
            if "Worker" in bases:
                found.append(path)
                break
    return sorted(set(found))


def _reachable_from(seeds: list[Path]) -> dict[str, Path]:
    """Every module in `app/` reachable from these, transitively."""
    seen: dict[str, Path] = {}
    queue = list(seeds)
    while queue:
        path = queue.pop()
        dotted = _dotted(path)
        if dotted in seen:
            continue
        seen[dotted] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imports(tree, dotted):
            target = _module_path(imported)
            if target is not None and _dotted(target) not in seen:
                queue.append(target)
    return seen


def test_the_autonomous_surface_is_found_at_all() -> None:
    """The audit below is worthless if it walks an empty graph."""
    seeds = _autonomous_entry_points()
    assert len(seeds) >= 3, f"only found {len(seeds)} worker modules"
    names = {p.name for p in seeds}
    assert "worker.py" in names
    reachable = _reachable_from(seeds)
    assert len(reachable) > 30, f"only {len(reachable)} modules reachable; graph looks broken"


# ====================================================== Phase 30: the audit


#: Calls that change WHAT IS PERMITTED rather than acting within it. Reaching
#: any of these from unattended code would be an autonomous path to a decision
#: only a person may take.
FORBIDDEN_AUTONOMOUS_CALLS = {
    # Risk configuration and the switches that stop trading.
    "set_limits": "change risk limits",
    "release_kill_switch": "release a kill switch",
    "release_all": "release every safe-mode latch at once",
    # Model lifecycle.
    "promote": "promote a model version",
    "deploy_to_paper": "deploy a model version",
}


def test_no_autonomous_path_can_change_what_is_permitted() -> None:
    """**The L51 Phase 30 audit.**

    Walks the import graph from every `Worker` subclass and fails if unattended
    code can reach a call that changes what the platform is permitted to do.

    Deliberately NOT "a worker must not reach a venue": the execution worker
    reaching the OMS is the platform working, and the OMS is gated by the
    RiskEngine. The line is between acting within the permissions and changing
    them.
    """
    reachable = _reachable_from(_autonomous_entry_points())
    offenders: list[str] = []

    for dotted, path in sorted(reachable.items()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if name in FORBIDDEN_AUTONOMOUS_CALLS:
                offenders.append(
                    f"{dotted}:{node.lineno} -> {name} ({FORBIDDEN_AUTONOMOUS_CALLS[name]})"
                )

    assert offenders == [], (
        "unattended code can change what the platform is permitted to do: " + "; ".join(offenders)
    )


def test_the_phase_30_audit_is_capable_of_failing() -> None:
    """A guard that cannot fire is C-4 in another costume, and this file exists
    partly because of C-4. So the matcher is run against the shape it hunts."""
    planted = ast.parse("async def tick(self):\n    await self.risk.set_limits(db, wider)\n")
    hits = [
        node.func.attr
        for node in ast.walk(planted)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN_AUTONOMOUS_CALLS
    ]
    assert hits == ["set_limits"]


def test_the_trading_mode_is_never_reassigned_after_construction() -> None:
    """`TRADING_MODE` and `LIVE_TRADING` are configuration, not state.

    A constructor storing what it was handed is fine and is how `RiskService`
    receives them — with `paper` and `False` as its defaults. What must not
    exist is code that CHANGES either afterwards, because a mode that can move
    at runtime is a mode that can move for the wrong reason.

    So `__init__` is excluded and everything else is not. The first draft of
    this test flagged those two constructor lines, which is the difference
    between a matcher that finds a defect and one that finds a pattern.
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if func.name == "__init__":
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    targets: list[ast.expr] = list(node.targets)
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                else:
                    continue
                for target in targets:
                    if getattr(target, "attr", None) in ("trading_mode", "live_trading"):
                        offenders.append(f"{path.relative_to(APP)}:{node.lineno} in {func.name}()")
    assert offenders == [], (
        "something changes a trading-mode setting after construction: " + "; ".join(offenders)
    )


def test_the_risk_service_defaults_to_paper_and_not_live() -> None:
    """The default matters more than the assignment: a `RiskService` built with
    no arguments must not be a live one."""
    from app.risk.service import RiskService

    service = RiskService()
    assert service.trading_mode == "paper"
    assert service.live_trading is False


# ================================================ Phase 19: the budget itself


def test_the_recovery_budget_refuses_before_it_allows() -> None:
    """Both limits must pass, and each stops a different loop: `cooldown` a
    fast one, `max_attempts` a slow one."""
    from app.bots.budget import RecoveryBudget

    budget = RecoveryBudget(
        max_attempts=3, window=timedelta(hours=1), cooldown=timedelta(minutes=5)
    )
    from datetime import UTC, datetime

    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    # Nothing yet: allowed.
    assert budget.refusal(attempts_in_window=0, last_attempt_at=None, now=now) is None
    # Inside the cooldown: refused, even with budget left.
    assert (
        budget.refusal(attempts_in_window=1, last_attempt_at=now - timedelta(minutes=1), now=now)
        is not None
    )
    # Past the cooldown, budget left: allowed.
    assert (
        budget.refusal(attempts_in_window=2, last_attempt_at=now - timedelta(minutes=30), now=now)
        is None
    )
    # Budget spent: refused however long ago.
    assert (
        budget.refusal(attempts_in_window=3, last_attempt_at=now - timedelta(minutes=59), now=now)
        is not None
    )


@pytest.mark.parametrize("attempts", [0, 1, 2])
def test_the_budget_allows_genuine_transient_recovery(attempts: int) -> None:
    """The failure opposite to a restart loop: a budget so tight that automatic
    recovery never happens is a mechanism that only looks like one."""
    from datetime import UTC, datetime

    from app.bots.budget import RecoveryBudget

    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert (
        RecoveryBudget().refusal(
            attempts_in_window=attempts, last_attempt_at=now - timedelta(hours=2), now=now
        )
        is None
    )


# ================================ L51: starting the execution worker is a choice


def test_the_execution_worker_defaults_to_not_started() -> None:
    """**A worker that acts on trading defaults False, and must stay that way.**

    (Two flags carry that property now: this one and `oms_reconcile_enabled`,
    which decides an order's fate against the venue. The requirement is
    stated rather than the count, because the count went stale once.)

    `notifications_enabled` and `monitoring_enabled` default True because a
    platform whose monitoring starts only when somebody remembers to switch it
    on misses the first outage. The execution worker is the reverse: it acts on
    trading signals, so a platform that begins consuming them because somebody
    deployed it is a platform that traded without anybody deciding to.
    """
    from app.core.settings import Settings

    settings = Settings(_env_file=None)
    assert settings.execution_worker_enabled is False
    # And the flags it cannot change by being switched on.
    assert settings.trading_mode.value == "paper"
    assert settings.live_trading is False


def test_the_execution_worker_starts_after_the_recovery_sequence() -> None:
    """Ordering, asserted on the source rather than assumed.

    A worker that consumed a signal before startup reconciliation had run would
    act on a platform whose unresolved orders had not yet latched safe mode —
    and since the L45 C-1 fix that sequence can finally see those orders,
    because the pipeline now writes the rows it reads.
    """
    source = (APP / "main.py").read_text(encoding="utf-8")
    recovery = source.index("run_startup")
    started = source.index("app.state.workers.start(app.state.execution)")
    assert recovery < started, "the execution worker is started before the recovery sequence runs"


def test_starting_the_execution_worker_cannot_enable_live_trading() -> None:
    """Turning it on is an operator decision about *whether signals are acted
    on*, never about *what mode they are acted on in*."""
    from app.core.settings import Settings

    enabled = Settings(_env_file=None, execution_worker_enabled=True)
    assert enabled.execution_worker_enabled is True
    assert enabled.trading_mode.value == "paper"
    assert enabled.live_trading is False
    assert enabled.live_execution_allowed is False


# ==================== Tier-1 item 4: the reconcile sweep is also a choice


def test_the_oms_reconcile_sweep_defaults_to_not_started() -> None:
    """It sends nothing, and it still defaults off, because it DECIDES: a
    reconciliation that finds nothing at the venue writes `failed`, the one
    state a fresh order for the same intent may follow. Unblocking a resend
    on a timer is an operator's call, not a side effect of booting."""
    from app.core.settings import LIVE_GATES, Settings

    settings = Settings(_env_file=None)
    assert settings.oms_reconcile_enabled is False
    assert settings.oms_reconcile_interval_seconds >= 60.0
    assert settings.oms_reconcile_max_per_pass >= 1

    enabled = Settings(_env_file=None, oms_reconcile_enabled=True)
    assert enabled.oms_reconcile_enabled is True
    assert enabled.trading_mode.value == "paper"
    assert enabled.live_trading is False
    assert enabled.live_execution_allowed is False
    assert not any(LIVE_GATES.values())


def test_the_reconcile_sweep_starts_after_recovery_and_before_execution() -> None:
    """Ordering on the source. Startup reconciliation counts the unresolved
    orders and latches safe mode for them; the sweep is what can then clear
    them; the consumer of signals starts last."""
    source = (APP / "main.py").read_text(encoding="utf-8")
    recovery = source.index("run_startup")
    sweep = source.index("app.state.workers.start(app.state.oms_reconciler)")
    execution = source.index("app.state.workers.start(app.state.execution)")
    assert recovery < sweep < execution
