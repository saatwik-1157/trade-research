"""Which strategy-version status may follow which. **The L57 regression.**

`strategy_versions.status` is a lifecycle, and it was the one lifecycle in this
platform with no transition guard. `POST /v1/strategy-builder/.../status`
checked the DESTINATION — reachable, not blocked until a later level, and for
`validated` that the definition still parses — and never the (from, to) pair:

    version.status = wanted

So **`retired -> validated` was one call.** A retired strategy came back with
no re-validation of its dependencies, its model, or its approval — which is
exactly what L57 step 21 forbids: *"Do not blindly reactivate an old
strategy."*

Every other lifecycle here already has a transition table — `app/oms/state.py`,
`app/bots/state.py`, `app/risk/state.py`, `app/portfolio/state.py`. This is the
same pattern applied where it was missing, not a new mechanism.
"""

from __future__ import annotations

import pytest
from app.strategies.lifecycle import (
    TRANSITIONS,
    IllegalVersionTransition,
    VersionStatus,
    check_transition,
)

# ============================================================ the legal moves


@pytest.mark.parametrize(
    "current,wanted",
    [
        ("draft", "validated"),
        ("draft", "retired"),
        ("validated", "retired"),
        ("validated", "draft"),
    ],
)
def test_the_legal_moves_are_allowed(current: str, wanted: str) -> None:
    """The guard refuses what is unsafe, not everything.

    `validated -> draft` is deliberately legal: an operator who decides a
    version is not ready after all should not have to retire it to say so.
    """
    check_transition(current, wanted)


# ========================================================== the one that mattered


def test_a_retired_version_cannot_become_validated() -> None:
    """**The defect.** One call brought a retired strategy back.

    Reactivation must go through a NEW version — the only path that forces
    re-validation against the current platform and approval on its own
    evidence, rather than inheriting a decision made before it was retired.
    """
    with pytest.raises(IllegalVersionTransition) as exc:
        check_transition("retired", "validated")

    message = str(exc.value)
    assert "cannot become" in message
    assert "creating a NEW version" in message
    assert "own evidence" in message


def test_retired_is_terminal_in_every_direction() -> None:
    """Not just to `validated`. A retired version goes nowhere."""
    assert TRANSITIONS[VersionStatus.retired] == frozenset()
    for target in ("draft", "validated"):
        with pytest.raises(IllegalVersionTransition):
            check_transition("retired", target)


def test_retirement_does_not_delete_anything() -> None:
    """L57 step 20: retired means NO NEW DEPLOYMENT, not DELETE HISTORY.

    Read from the SYNTAX TREE, not the text. The first draft grepped the source
    for "delete" and failed on the module's own docstring, which says it never
    deletes anything — the same mistake the L45 C-4 guard made when it matched
    its own prose. A test that reads comments is testing the comments.
    """
    import ast
    import inspect

    from app.strategies import lifecycle

    tree = ast.parse(inspect.getsource(lifecycle))

    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("delete", "drop", "truncate", "execute", "commit"):
        assert forbidden not in called, f"the lifecycle module calls {forbidden}()"

    # It reaches no database at all: no session, no model, no query.
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [m for m in imported if "sqlalchemy" in m or "models" in m], (
        f"the lifecycle module reaches persistence: {sorted(imported)}"
    )


# ================================================================ the edges


def test_setting_a_status_to_itself_is_refused() -> None:
    """A no-op is not a transition. Accepting it would put a row in the audit
    trail recording a change that did not happen."""
    for status in ("draft", "validated", "retired"):
        with pytest.raises(IllegalVersionTransition, match="already"):
            check_transition(status, status)


def test_an_unknown_status_is_refused_and_names_the_real_ones() -> None:
    """`active` and `paper` are statuses the route blocks until the machinery
    that enforces them exists. They must not slip in through here either."""
    for bogus in ("active", "paper", "backtested", "paused", "", "APPROVED"):
        with pytest.raises(IllegalVersionTransition) as exc:
            check_transition("draft", bogus)
        assert "draft, validated, retired" in str(exc.value)


def test_the_statuses_match_the_database_constraint() -> None:
    """The enum and the CHECK constraint must not drift apart. A status the
    enum allows and the column refuses is an insert that fails at commit."""
    from app.models.strategies import StrategyVersion

    checks = [c for c in StrategyVersion.__table__.constraints if hasattr(c, "sqltext")]
    text = " ".join(str(c.sqltext) for c in checks)
    for status in VersionStatus:
        assert f"'{status.value}'" in text, f"{status} is not in the CHECK constraint"


def test_every_status_has_a_transition_entry() -> None:
    """A status missing from the table would raise `KeyError` inside the guard
    — a crash instead of a refusal, on the safety path."""
    assert set(TRANSITIONS) == set(VersionStatus)


# ================================================ the guard is actually wired


def test_the_route_checks_the_transition_before_it_writes() -> None:
    """The defect one level up: a guard that exists and is not called.

    Asserted on the source because that is where it went wrong — the checks on
    the DESTINATION were all present and correct, and the (from, to) pair was
    simply never consulted.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "strategy_builder.py"
    ).read_text(encoding="utf-8")

    assert "check_transition(version.status, wanted)" in source, (
        "the status route no longer checks the transition"
    )

    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "check_transition" in called

    # And it runs BEFORE the assignment, or it guards nothing.
    guard_at = source.index("check_transition(version.status, wanted)")
    write_at = source.index("version.status = wanted")
    assert guard_at < write_at, "the transition is checked after it is written"
