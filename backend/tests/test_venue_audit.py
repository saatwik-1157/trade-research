"""Comparing the platform's record of a trade against the venue's.

Every other check in this suite compares the platform against itself. The
comparison this module performs is the only one that can catch a booking that
was wrong in a way both sides of the platform agreed about -- which is exactly
what happened to `realized_pnl` before 2026-09-07: the row said 0.0000, every
reader of the row agreed, and the account had received -0.04.

`compare` is a pure function over two sides, so it is tested without a database
and without a terminal. What it cannot test is whether MetaTrader's history was
read correctly; that is `read_venue`, and it needs a terminal.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.brokers.venue_audit import (
    MONEY_TOLERANCE,
    PRICE_TOLERANCE,
    VenueTrade,
    compare,
)


def _ours(**over: object) -> SimpleNamespace:
    """A `positions` row, shaped by what `compare` reads."""
    base = {
        "broker_position_id": "58332563074",
        "entry_price": Decimal("1.16237"),
        "initial_quantity": Decimal("0.01"),
        "quantity": Decimal("0"),
        "status": "closed",
        "realized_pnl": Decimal("0.02"),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _theirs(**over: object) -> VenueTrade:
    base = {
        "position_id": "58332563074",
        "entry_price": Decimal("1.16237"),
        "volume": Decimal("0.01"),
        "exit_price": Decimal("1.16239"),
        "realized": Decimal("0.02"),
        "closed": True,
        "deals": 2,
    }
    base.update(over)
    return VenueTrade(**base)  # type: ignore[arg-type]


def _report(ours: SimpleNamespace, theirs: VenueTrade | None):  # noqa: ANN202
    venue = {} if theirs is None else {theirs.position_id: theirs}
    return compare([ours], venue)  # type: ignore[list-item]


# ------------------------------------------------------------------ agrees


def test_a_trade_the_venue_confirms_agrees() -> None:
    report = _report(_ours(), _theirs())
    assert report.checked == 1
    assert report.agreed == 1
    assert report.clean is True
    assert report.findings == []


def test_dust_below_the_price_tolerance_is_not_a_disagreement() -> None:
    """Both sides store more digits than any instrument quotes."""
    report = _report(
        _ours(entry_price=Decimal("1.16237")),
        _theirs(entry_price=Decimal("1.16237") + PRICE_TOLERANCE / 2),
    )
    assert report.clean is True


def test_a_cent_of_money_difference_is_tolerated_and_more_is_not() -> None:
    within = _report(
        _ours(realized_pnl=Decimal("0.02")),
        _theirs(realized=Decimal("0.02") + MONEY_TOLERANCE / 2),
    )
    assert within.clean is True

    beyond = _report(_ours(realized_pnl=Decimal("0.02")), _theirs(realized=Decimal("0.50")))
    assert beyond.clean is False
    assert beyond.findings[0].field == "realized_pnl"


# -------------------------------------------------------------- disagrees


def test_the_money_defect_this_module_exists_for() -> None:
    """Position 58328827918, measured on 2026-09-07.

    The row said 0.0000 because the booking was a price difference with no
    contract size. The account received -0.04. Nothing inside the platform
    could see it, because every reader trusted the row.
    """
    report = _report(
        _ours(broker_position_id="58328827918", realized_pnl=Decimal("0.0000")),
        _theirs(position_id="58328827918", realized=Decimal("-0.04")),
    )
    assert report.agreed == 0
    finding = report.findings[0]
    assert finding.field == "realized_pnl"
    assert finding.ours == "0.0000"
    assert finding.theirs == "-0.04"


def test_a_gap_is_reported_as_a_gap_not_as_a_zero() -> None:
    """`realized_pnl` NULL against money at the venue is a distinct finding.

    A close whose money the venue did not report books nothing on purpose. That
    is recoverable and it is not the same as booking the wrong number, so it
    says so rather than being folded into a value mismatch.
    """
    report = _report(_ours(realized_pnl=None), _theirs(realized=Decimal("0.33")))
    finding = report.findings[0]
    assert finding.ours is None
    assert "a gap, not a zero" in finding.detail


def test_disagreeing_about_whether_it_is_finished() -> None:
    """The harness closed a platform position out of band, and the row stayed open."""
    report = _report(
        _ours(status="open", realized_pnl=None),
        _theirs(closed=True, realized=Decimal("0.54")),
    )
    fields = {f.field for f in report.findings}
    assert "status" in fields


def test_an_entry_the_venue_did_not_fill_at() -> None:
    report = _report(_ours(entry_price=Decimal("1.16000")), _theirs())
    assert report.findings[0].field == "entry_price"


def test_a_size_the_venue_did_not_fill() -> None:
    report = _report(_ours(initial_quantity=Decimal("0.05")), _theirs())
    assert report.findings[0].field == "volume"


def test_one_position_can_disagree_about_several_things() -> None:
    report = _report(
        _ours(entry_price=Decimal("1.10000"), initial_quantity=Decimal("0.05"), status="open"),
        _theirs(),
    )
    # Three, not four: the money still matches, and a field that agrees is not
    # reported just because its neighbours did not.
    assert {f.field for f in report.findings} == {"entry_price", "volume", "status"}
    assert report.agreed == 0


# ------------------------------------------------------ what it will not do


def test_a_position_the_venue_never_heard_of_is_not_a_value_disagreement() -> None:
    """Bigger than a mismatched field, and reported separately.

    It means the platform believes in a trade the venue has no record of, or
    that the history window is too short. Folding it in with "the price differs"
    would bury it.
    """
    report = _report(_ours(), None)
    assert report.findings == []
    assert report.unknown_to_venue == ["58332563074"]
    assert report.clean is False


def test_an_open_position_has_no_money_to_compare() -> None:
    """Nothing is realised until the venue closes it, so nothing is compared."""
    report = _report(
        _ours(status="open", realized_pnl=None),
        _theirs(closed=False, realized=None),
    )
    assert report.clean is True


@pytest.mark.parametrize("attr", ["entry_price", "initial_quantity"])
def test_a_missing_value_on_our_side_is_skipped_not_guessed(attr: str) -> None:
    """A field we never recorded is not evidence of a disagreement.

    `unknown_to_venue` covers "we have a trade they do not". A null column is a
    different thing and reporting it as a mismatch would put noise in front of
    the findings that matter.
    """
    ours = _ours(**{attr: None})
    if attr == "initial_quantity":
        ours.quantity = None
    report = _report(ours, _theirs())
    assert report.clean is True


def test_the_audit_reads_and_never_writes() -> None:
    """It reports. Correcting either side is a decision for a person."""
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "brokers" / "venue_audit.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("add", "commit", "delete", "merge", "flush", "order_send"):
        assert forbidden not in called, f"venue_audit calls {forbidden}"
