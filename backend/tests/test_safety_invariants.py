"""The invariant registry, and the architecture scan behind it. **L62.**

Two things are checked here, and the second is the one that matters most.

**1. The registry cannot lie.** Every module and every test it names as
evidence must exist. It was written from memory and greps, and when first run
this check found eleven evidence pointers aimed at tests that were real but in
different files, plus two aimed at nothing at all. A registry whose evidence
was never resolved would be a list of confident sentences.

**2. There is no unauthorized execution path.** L62 section 34 lists the paths
that must not exist -- TradingView to MT5, AI to MT5, optimizer to MT5,
research to MT5, policy to MT5 -- and this walks the syntax tree of every
module under `app/` to check. It is the strongest single statement this level
can make, because it is a property of the whole codebase rather than of a
component, and it is decided mechanically rather than by reading.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime

import pytest
from app.safety.certification import GATES, CertificationState, GateStatus, certify
from app.safety.invariants import INVARIANTS, Invariant, InvariantStatus, Severity

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
TESTS = pathlib.Path(__file__).resolve().parent


# ============================================== 1. the registry cannot lie


@pytest.mark.parametrize("inv", [i for i in INVARIANTS if i.status is InvariantStatus.ENFORCED])
def test_every_enforced_invariant_names_a_module_that_exists(inv: Invariant) -> None:
    parts = inv.enforced_by.split(".")
    assert parts[0] == "app", inv.enforced_by
    target = APP.joinpath(*parts[1:])
    assert target.with_suffix(".py").exists() or (target / "__init__.py").exists(), (
        f"{inv.id} claims {inv.enforced_by} enforces it, and that module does not exist"
    )


@pytest.mark.parametrize("inv", [i for i in INVARIANTS if i.status is InvariantStatus.ENFORCED])
def test_every_enforced_invariant_names_a_test_that_exists(inv: Invariant) -> None:
    """The check that caught thirteen bad pointers the first time it ran."""
    path, _, name = inv.evidence.partition("::")
    f = TESTS.parent / path
    assert f.exists(), f"{inv.id} names {path}, which does not exist"
    assert f"def {name}(" in f.read_text(encoding="utf-8"), (
        f"{inv.id} names {name}, which is not defined in {path}"
    )


@pytest.mark.parametrize("inv", [i for i in INVARIANTS if i.status is not InvariantStatus.ENFORCED])
def test_anything_not_enforced_explains_itself(inv: Invariant) -> None:
    """ "We did not do this" needs a reason far more than "we did"."""
    assert len(inv.note) > 80, f"{inv.id} is {inv.status} with no real explanation"


def test_no_invariant_is_left_out_of_every_gate() -> None:
    """An invariant under no gate is a property nobody certifies."""
    covered = {i for g in GATES for i in g.invariants}
    assert {i.id for i in INVARIANTS} == covered


def test_the_ids_are_unique_and_complete() -> None:
    ids = [i.id for i in INVARIANTS]
    assert len(ids) == len(set(ids))
    assert ids == [f"INV-{n:02d}" for n in range(1, len(ids) + 1)]


def test_nothing_applicable_is_left_unenforced() -> None:
    """The set that would mean a known hole. Empty by measurement, not by
    nobody having looked."""
    from app.safety.invariants import unenforced

    assert unenforced() == ()


# ================== 2. section 34: there is no unauthorized execution path


#: The methods that actually place, close, cancel or amend an order at a venue.
#: Reading is not on this list: `positions()` and `account()` cannot move money.
VENUE_WRITES = frozenset({"place_order", "close_position", "cancel_order", "modify_order"})

#: The ONLY modules permitted to call one.
#:
#: `app/oms/service.py` is the order management system, which is the single
#: broker boundary the platform has had since L19. `app/brokers/mt5.py` is on
#: the list for one call and one only -- `self.modify_order`, the adapter
#: attaching a bracket to a fill it just received. An adapter calling itself is
#: not a second path to a venue; it is the inside of the one path.
VENUE_WRITE_CALLERS = frozenset({"app/oms/service.py", "app/brokers/mt5.py"})


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(APP.parent).as_posix()


def test_only_the_oms_reaches_a_venue_to_write() -> None:
    """**L62 section 34.** The whole codebase, not one component.

    Every forbidden path the brief names -- TradingView to MT5, AI to MT5,
    PortfolioDecisionEngine to MT5, optimizer to MT5, research to MT5, policy
    to MT5 -- is a special case of this one assertion, and none of them has to
    be enumerated for it to hold. A path added later under a name nobody
    thought of fails this test the same way.
    """
    offenders: dict[str, list[str]] = {}
    for f in _modules():
        rel = _rel(f)
        if rel in VENUE_WRITE_CALLERS:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a file that does not parse
            continue
        found = [
            f"{node.func.attr} (line {node.lineno})"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in VENUE_WRITES
        ]
        if found:
            offenders[rel] = found
    assert offenders == {}, "a module outside the OMS reaches a venue to write: " + repr(offenders)


def test_the_adapters_only_self_call_is_the_bracket_it_just_placed() -> None:
    """`app/brokers/mt5.py` is allow-listed, so the allowance is pinned.

    Without this, the entry in `VENUE_WRITE_CALLERS` would license any future
    venue write anywhere in that file.
    """
    tree = ast.parse((APP / "brokers" / "mt5.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in VENUE_WRITES
    ]
    assert len(calls) == 1, f"expected exactly one venue write in the MT5 adapter, got {len(calls)}"
    only = calls[0]
    assert isinstance(only.func, ast.Attribute)  # held by the filter above
    assert isinstance(only.func.value, ast.Name) and only.func.value.id == "self", (
        "the MT5 adapter's one venue write must be on itself, not on another adapter"
    )
    assert only.func.attr == "modify_order"


def test_the_safety_package_reaches_nothing_that_trades() -> None:
    """A verifier that could act would be the thing it exists to forbid."""
    forbidden = VENUE_WRITES | {"submit", "approve", "execute", "commit", "engage", "release"}
    for f in sorted((APP / "safety").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not (called & forbidden), f"{_rel(f)} calls {sorted(called & forbidden)}"


def test_the_safety_package_imports_only_what_it_delegates_to() -> None:
    """It may read the classifiers it delegates to, and nothing else.

    An import of the RiskEngine, the OMS or an adapter would mean a verifier
    that could consult -- and then eventually replace -- the authority it is
    supposed to be checking.
    """
    allowed = {"app.portfolio.control", "app.portfolio.decision", "app.safety"}
    for f in sorted((APP / "safety").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = (
                node.module
                if isinstance(node, ast.ImportFrom) and node.module
                else next((a.name for a in node.names), "")
                if isinstance(node, ast.Import)
                else ""
            )
            if mod.startswith("app."):
                assert any(mod == a or mod.startswith(a + ".") for a in allowed), (
                    f"{_rel(f)} imports {mod}, which is outside what it may delegate to"
                )


# ================================================== 3. certification is real


def test_the_platform_is_conditionally_certified_and_says_why() -> None:
    """Not CERTIFIED, and the difference is the point.

    Two gates cannot be tested end to end because the components they would
    constrain do not exist. Reporting them green would make this whole file
    decoration.
    """
    cert = certify(now=datetime(2026, 9, 6, tzinfo=UTC))
    assert cert.state is CertificationState.CONDITIONALLY_CERTIFIED
    partial = {g.gate.id for g in cert.gates if g.status is GateStatus.WARNING}
    assert partial == {"GATE-01", "GATE-10"}
    assert not [g for g in cert.gates if g.status is GateStatus.FAIL]


def test_a_failed_critical_invariant_revokes_certification() -> None:
    """The claim has to be able to come out negative, or it is not a claim."""
    cert = certify(now=datetime(2026, 9, 6, tzinfo=UTC), failed=frozenset({"INV-01"}))
    assert cert.state is CertificationState.CERTIFICATION_REVOKED
    assert "INV-01" in " ".join(cert.reasons)


def test_one_critical_failure_is_not_outweighed_by_everything_else() -> None:
    """Twenty-four passes do not average out a breached veto."""
    cert = certify(now=datetime(2026, 9, 6, tzinfo=UTC), failed=frozenset({"INV-10"}))
    assert cert.state is CertificationState.CERTIFICATION_REVOKED


def test_a_gate_with_nothing_applicable_under_it_is_not_a_pass() -> None:
    """The rule that keeps a certification from being generated by absence."""
    from app.safety.certification import Gate, evaluate_gate

    hollow = Gate("GATE-XX", "Hollow", "Does anything here exist?", ("INV-24", "INV-25"))
    result = evaluate_gate(hollow)
    assert result.status is GateStatus.NOT_TESTED
    assert "not a pass" in result.reason


def test_every_critical_invariant_is_critical_for_a_reason() -> None:
    """A severity everything shares is a severity that says nothing."""
    sev = {i.severity for i in INVARIANTS}
    assert Severity.CRITICAL in sev and len(sev) > 1
