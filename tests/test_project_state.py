"""`PROJECT_STATE.json` must be measured, not remembered.

The file was hand-written and carried `"generated_by": "hand"`. By 2026-09-06
it had drifted from the database on six counts at once: it reported 0 datasets
against 1 READY dataset of 638 rows, 0 training runs against 3, 0 model
versions against 1, and repeated the claim that `market_bars` was empty when
the table held 705 bars.

**None of those drifts was careless.** Each was true when written. A
hand-written state file is a photograph presented as a window, and the only
durable fix is to take the photograph on demand.

These tests do not check the figures -- the database is not available here, and
a test that needed one would be skipped in CI and therefore useless. They check
the two things that make the figures trustworthy: that the file says where it
came from, and that the generator reports an unmeasurable figure as absent
rather than as zero.

Run: python tests/test_project_state.py
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import project_state  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name}  {detail}")


def test_the_state_file_says_it_was_generated() -> None:
    """A hand-edit shows up here, because a person editing the file does not
    also update this field to lie about it."""
    state = json.loads((ROOT / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    check(
        "generated_by names the generator",
        state.get("generated_by") == "tools/project_state.py",
        f"got {state.get('generated_by')!r}",
    )
    check("generated_at is present", bool(state.get("generated_at")))
    check(
        "measurement_source is named",
        "tr-postgres" in str(state.get("measurement_source", "")),
    )


def test_every_figure_names_the_query_behind_it() -> None:
    """CLAUDE.md's rule applied to the state file: a figure the reader cannot
    trace is a figure the reader has to trust."""
    state = json.loads((ROOT / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    measured = state.get("measured", {})
    provenance = state.get("provenance", {})
    missing = sorted(set(measured) - set(provenance))
    check("every measured figure has a query", not missing, f"missing: {missing}")


def test_an_unreachable_database_reports_absence_not_zero() -> None:
    """**The rule that matters most.**

    Zero is a measurement -- "there are no datasets". An unreachable database
    reported as zero would be the fabrication the rest of the platform refuses,
    and it is the easy bug: `int(out or 0)` is a natural thing to write.
    """
    original = project_state.psql
    try:
        project_state.psql = lambda sql, **kw: None  # type: ignore[assignment]
        state = project_state.build()
    finally:
        project_state.psql = original  # type: ignore[assignment]

    check("no figures are invented", state["measured"] == {}, str(state["measured"]))
    check(
        "every figure is named as unmeasured",
        set(state["unmeasured"]) == set(project_state.COUNTS),
        f"got {len(state['unmeasured'])} of {len(project_state.COUNTS)}",
    )
    check("schema head is null, not a guess", state["schema_head"] is None)


def test_the_market_data_note_states_a_requirement_not_a_count() -> None:
    """The phrasing that caused the drift.

    "`market_bars` is empty" is a claim about a mutable table and went stale on
    the first ingestion -- it appeared in 69 lines across 35 files, all of them
    true when written. A note that states the REQUIREMENT ("correlation needs
    two or more instruments") stays true until the thing that actually matters
    changes.
    """
    state = json.loads((ROOT / "PROJECT_STATE.json").read_text(encoding="utf-8"))
    note = str(state.get("notes", {}).get("market_data", ""))
    check("the note names the requirement", "TWO OR MORE" in note.upper(), note[:60])
    check(
        "the note warns against the stale phrasing",
        "goes stale" in note,
        note[:60],
    )


def main() -> int:
    print(__doc__.strip().splitlines()[0])
    for fn in (
        test_the_state_file_says_it_was_generated,
        test_every_figure_names_the_query_behind_it,
        test_an_unreachable_database_reports_absence_not_zero,
        test_the_market_data_note_states_a_requirement_not_a_count,
    ):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
