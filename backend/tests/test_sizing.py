"""The Position Sizing Engine (L18).

`app.sizing` existed before this level as arithmetic with one consumer and no
tests of its own -- `tests/test_paper.py` proved the paper engine *called* it,
never that it computed correctly. This file is its first direct test, and it
covers the thirty cases the level brief names.

Two of these tests pin a behaviour that CHANGED at L18, and they are the
reason the change was made:

  * `test_a_size_below_the_venue_minimum_is_refused_not_raised` -- the module
    used to raise the volume to the broker's minimum and record the overshoot
    in a `gap` string no caller was obliged to read. That silently risks more
    than the budget, which is the one thing sizing exists to prevent.
  * `test_a_stop_on_the_wrong_side_is_refused` -- direction was not validated
    at all, because the module only ever saw a distance.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path

import pytest
from app.sizing.calculator import (
    METHOD_ALIASES,
    SizingError,
    SizingMethod,
    SizingRequest,
    SizingStatus,
    calculate,
    resolve_method,
)
from app.sizing.service import SizingService
from app.symbols.service import ContractSpec

# ================================================================= fixtures


def spec(**overrides: object) -> ContractSpec:
    """EURUSD at this project's broker. 0.01 lots, 0.01 step, five digits."""
    base: dict[str, object] = {
        "internal_symbol": "EURUSD",
        "broker_symbol": "EURUSD",
        "provider": "simulator",
        "contract_size": Decimal("100000"),
        "tick_size": Decimal("0.00001"),
        "tick_value": Decimal("1"),
        "minimum_volume": Decimal("0.01"),
        "maximum_volume": Decimal("100"),
        "volume_step": Decimal("0.01"),
        "price_precision": 5,
        "volume_precision": 2,
        "trading_hours": None,
        "spec_source": "test",
        "spec_updated_at": None,
    }
    base.update(overrides)
    return ContractSpec(**base)  # type: ignore[arg-type]


def equity_spec(**overrides: object) -> ContractSpec:
    """The brief's worked example: an instrument priced like a share.

    Tick size 0.01, tick value 0.01, so one unit of volume moving 1.00 of
    price is worth 1.00 of account currency and the arithmetic in section 6
    of the level brief can be checked literally.
    """
    base: dict[str, object] = {
        "internal_symbol": "ACME",
        "broker_symbol": "ACME",
        "provider": "simulator",
        "contract_size": Decimal("1"),
        "tick_size": Decimal("0.01"),
        "tick_value": Decimal("0.01"),
        "minimum_volume": Decimal("1"),
        "maximum_volume": Decimal("100000"),
        "volume_step": Decimal("1"),
        "price_precision": 2,
        "volume_precision": 0,
        "trading_hours": None,
        "spec_source": "test",
        "spec_updated_at": None,
    }
    base.update(overrides)
    return ContractSpec(**base)  # type: ignore[arg-type]


# ============================================== 1-2. fixed quantity and lot


def test_fixed_quantity_returns_what_was_asked_for() -> None:
    result = calculate(
        SizingRequest(method=SizingMethod.fixed_quantity, spec=spec(), quantity=Decimal("0.50"))
    )
    assert result.status is SizingStatus.valid
    assert result.volume == Decimal("0.50")
    # No risk budget was stated, so no risk figure is invented.
    assert result.risk_requested is None
    assert result.risk_actual is None


def test_fixed_lot_is_the_same_mode_under_the_brief_s_name() -> None:
    """`fixed_lot` is an alias, not a fourth branch. On every instrument this
    platform trades the quantity IS the lot, so a second mode would be a
    second name for one calculation."""
    assert resolve_method("fixed_lot") is SizingMethod.fixed_quantity
    assert resolve_method("monetary_risk") is SizingMethod.fixed_risk
    assert resolve_method("percentage_of_equity") is SizingMethod.percent_equity
    for alias in METHOD_ALIASES:
        assert isinstance(resolve_method(alias), SizingMethod)


def test_an_unknown_mode_is_refused_and_names_the_known_ones() -> None:
    with pytest.raises(SizingError) as exc:
        resolve_method("kelly")
    assert "kelly" in str(exc.value)
    assert "fixed_quantity" in str(exc.value)


# ====================================================== 3. equity percentage


def test_percent_equity_sizes_from_measured_equity() -> None:
    """10,000 at 1% is a 100 budget; a 0.005 stop on EURUSD costs 500 per
    lot, so the answer is 0.20 lots risking exactly 100."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.percent_equity,
            spec=spec(),
            equity=Decimal("10000"),
            risk_percent=Decimal("1"),
            side="buy",
            entry_price=Decimal("1.10000"),
            stop_loss=Decimal("1.09500"),
        )
    )
    assert result.volume == Decimal("0.20")
    assert result.risk_requested == Decimal("100.0000")
    assert result.risk_actual == Decimal("100.0000")
    assert result.risk_per_unit == Decimal("500.0000")


def test_a_risk_percent_above_one_hundred_is_refused() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.percent_equity,
            spec=spec(),
            equity=Decimal("10000"),
            risk_percent=Decimal("101"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused
    assert "exceeds 100" in (result.gap or "")


# ========================================================== 4. monetary risk


def test_fixed_risk_sizes_from_a_sum_of_account_currency() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("250"),
            stop_distance=Decimal("0.005"),
        )
    )
    # 250 / 500 per lot = 0.5 lots.
    assert result.volume == Decimal("0.50")
    assert result.risk_actual == Decimal("250.0000")


# ================================================= 5. stop-loss based sizing


def test_the_briefs_worked_example_computes_exactly_fifty_units() -> None:
    """Section 6 of the level brief, checked literally.

    Entry 100, stop 98, tick size 0.01, tick value 0.01, risk 100.
    200 ticks of stop distance x 0.01 = 2.00 per unit; 100 / 2 = 50.
    """
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side="long",
            entry_price=Decimal("100"),
            stop_loss=Decimal("98"),
        )
    )
    assert result.stop_distance == Decimal("2")
    assert result.risk_per_unit == Decimal("2.0000")
    assert result.volume == Decimal("50")
    assert result.risk_actual == Decimal("100.0000")


def test_a_stop_distance_is_required_for_every_risk_mode() -> None:
    for method, extra in (
        (SizingMethod.fixed_risk, {"risk_amount": Decimal("100")}),
        (
            SizingMethod.percent_equity,
            {"equity": Decimal("10000"), "risk_percent": Decimal("1")},
        ),
    ):
        result = calculate(SizingRequest(method=method, spec=spec(), **extra))  # type: ignore[arg-type]
        assert result.refused
        assert "stop distance" in (result.gap or "")


def test_a_stop_distance_that_disagrees_with_the_levels_is_refused() -> None:
    """Two statements of one fact. Choosing either would size a trade the
    caller did not describe, so both are refused."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side="long",
            entry_price=Decimal("100"),
            stop_loss=Decimal("98"),
            stop_distance=Decimal("5"),
        )
    )
    assert result.refused
    assert "disagrees" in (result.gap or "")


# ================================================== 6-8. long, short, invalid


def test_a_long_and_a_short_of_the_same_geometry_size_identically() -> None:
    long = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side="long",
            entry_price=Decimal("100"),
            stop_loss=Decimal("98"),
        )
    )
    short = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side="short",
            entry_price=Decimal("100"),
            stop_loss=Decimal("102"),
        )
    )
    assert long.volume == short.volume == Decimal("50")
    assert long.side == "long"
    assert short.side == "short"


@pytest.mark.parametrize(
    ("side", "entry", "stop"),
    [
        ("long", "100", "102"),  # a long stop above its entry is a target
        ("buy", "100", "102"),
        ("short", "100", "98"),  # a short stop below its entry is a target
        ("sell", "100", "98"),
    ],
)
def test_a_stop_on_the_wrong_side_is_refused(side: str, entry: str, stop: str) -> None:
    """CHANGED AT L18. The module used to see only a distance, so it could not
    tell a stop from a target. Neither correction is safe -- flipping the stop
    and flipping the side both change what was asked for -- so it refuses."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side=side,
            entry_price=Decimal(entry),
            stop_loss=Decimal(stop),
        )
    )
    assert result.refused
    assert "must sit" in (result.gap or "")
    assert "target" in (result.gap or "")


def test_an_equal_entry_and_stop_is_refused() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side="long",
            entry_price=Decimal("100"),
            stop_loss=Decimal("100"),
        )
    )
    assert result.refused
    assert "no denominator" in (result.gap or "")


def test_an_unknown_side_is_refused_rather_than_assumed_long() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            side="upwards",
            entry_price=Decimal("100"),
            stop_loss=Decimal("98"),
        )
    )
    assert result.refused
    assert "unknown side" in (result.gap or "")


def test_direction_is_validated_even_when_the_size_is_fixed() -> None:
    """A fixed quantity does not come from the bracket, but a broken bracket
    still reaches the OMS attached to the order."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_quantity,
            spec=equity_spec(),
            quantity=Decimal("10"),
            side="long",
            entry_price=Decimal("100"),
            stop_loss=Decimal("102"),
        )
    )
    assert result.refused


# ============================================ 9-11. zero, negative, no equity


@pytest.mark.parametrize("value", ["0", "-1", "-0.0001"])
def test_a_non_positive_risk_amount_is_refused(value: str) -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal(value),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused
    assert "positive risk_amount" in (result.gap or "")


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_risk_percent_is_refused(value: str) -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.percent_equity,
            spec=spec(),
            equity=Decimal("10000"),
            risk_percent=Decimal(value),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused


@pytest.mark.parametrize("value", ["0", "-100"])
def test_a_non_positive_equity_is_refused(value: str) -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.percent_equity,
            spec=spec(),
            equity=Decimal(value),
            risk_percent=Decimal("1"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused
    assert "positive equity" in (result.gap or "")


def test_a_budget_that_rounds_to_zero_is_refused() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.percent_equity,
            spec=spec(),
            equity=Decimal("0.0001"),
            risk_percent=Decimal("0.0001"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused
    assert "rounds to zero" in (result.gap or "")


# ================================================== 12-13. tick size / value


@pytest.mark.parametrize("field", ["tick_size", "tick_value"])
def test_a_missing_tick_figure_refuses_rather_than_defaults(field: str) -> None:
    """The module's founding rule, inherited from `lot_for_risk`: a lot sized
    from an absent tick value is a real order for the wrong amount."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(**{field: None}),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused
    assert field in (result.gap or "")


@pytest.mark.parametrize("field", ["tick_size", "tick_value"])
def test_a_non_positive_tick_figure_refuses(field: str) -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(**{field: Decimal("0")}),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused


def test_tick_value_is_not_assumed_to_equal_tick_size() -> None:
    """Two instruments with identical prices and stops size differently when
    their tick value differs. Assuming stock-like behaviour is the error this
    guards -- the same 100 budget buys half as much at twice the tick value."""
    cheap = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("2"),
        )
    )
    dear = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=equity_spec(tick_value=Decimal("0.02")),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("2"),
        )
    )
    assert cheap.volume == Decimal("50")
    assert dear.volume == Decimal("25")


# ============================================== 14-17. steps, bounds, floor


def test_the_volume_step_is_respected_and_always_floors() -> None:
    """0.137 at a 0.01 step is 0.13, never 0.14. Rounding up risks more than
    was budgeted, which is the one thing sizing exists to prevent."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            # 68.5 / 500 = 0.137
            spec=spec(),
            risk_amount=Decimal("68.5"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.raw_volume == Decimal("0.137")
    assert result.volume == Decimal("0.13")
    assert result.risk_actual == Decimal("65.0000")
    assert result.risk_actual < result.risk_requested  # type: ignore[operator]
    assert any("floored" in w for w in result.warnings)


def test_a_size_below_the_venue_minimum_is_refused_not_raised() -> None:
    """CHANGED AT L18, and this is the safety fix of the level.

    The module used to set the volume to the broker minimum and record the
    overshoot in a `gap` string a caller was free to ignore. A 1.00 budget on
    an instrument whose minimum lot risks 5.00 is not a 1.00 trade; refusing
    is the only answer that keeps the configured risk true.
    """
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("1"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.refused
    assert result.volume is None
    assert "below the venue minimum" in (result.gap or "")
    # The refusal says what it would have cost, so an operator can tell a
    # coarse instrument from a risk setting that is too small.
    assert "5.0000" in (result.gap or "")
    assert "binding constraint" in (result.gap or "")


def test_a_fixed_quantity_below_the_minimum_is_refused_too() -> None:
    result = calculate(
        SizingRequest(method=SizingMethod.fixed_quantity, spec=spec(), quantity=Decimal("0.005"))
    )
    assert result.refused
    assert "minimum" in (result.gap or "")


def test_a_size_above_the_venue_maximum_is_capped_with_a_warning() -> None:
    """The maximum binds DOWNWARD, which can only reduce risk, so it is
    applied and reported. The asymmetry with the minimum is deliberate."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(maximum_volume=Decimal("0.10")),
            risk_amount=Decimal("250"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.volume == Decimal("0.10")
    assert any("maximum" in w for w in result.warnings)
    assert result.risk_actual == Decimal("50.0000")
    assert result.risk_actual < result.risk_requested  # type: ignore[operator]


def test_the_step_count_is_rounded_before_flooring() -> None:
    """The float-floor defect this repository already paid for: a budget worth
    exactly five steps arrives as 4.999999999 and a bare floor drops one."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            # 24.9999999999 / 500 = 0.0499999999998, which is 5 steps that
            # lost their last bits, not 4.
            risk_amount=Decimal("24.9999999999"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.volume == Decimal("0.05")


# ================================================ 18. actual risk after round


def test_actual_risk_is_recalculated_after_rounding_and_both_are_returned() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("68.5"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.risk_requested == Decimal("68.5")
    assert result.risk_actual == Decimal("65.0000")
    payload = result.as_dict()
    assert payload["risk_amount"] == "68.5"
    assert payload["actual_risk"] == "65.0000"
    assert payload["raw_quantity"] == "0.137"
    assert payload["final_quantity"] == "0.13"


def test_actual_risk_never_exceeds_the_budget_across_many_shapes() -> None:
    """The invariant, checked over a grid rather than one case."""
    for budget in ("5", "12.34", "68.5", "100", "999.99", "10000"):
        for distance in ("0.0001", "0.005", "0.01234", "0.5"):
            result = calculate(
                SizingRequest(
                    method=SizingMethod.fixed_risk,
                    spec=spec(),
                    risk_amount=Decimal(budget),
                    stop_distance=Decimal(distance),
                )
            )
            if result.ok:
                assert result.risk_actual is not None
                assert result.risk_actual <= Decimal(budget), (budget, distance)


# ================================================ 19-20. risk ceiling, margin


def test_a_budget_above_the_permitted_maximum_is_refused() -> None:
    """The Risk Engine's ceiling, applied before sizing. Refusing rather than
    quietly sizing to the smaller figure: the caller asked for a trade this
    account may not take, and that is worth saying."""
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("500"),
            stop_distance=Decimal("0.005"),
            max_risk_amount=Decimal("100"),
        )
    )
    assert result.refused
    assert "permitted maximum" in (result.gap or "")


def test_a_budget_within_the_permitted_maximum_sizes_normally() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("50"),
            stop_distance=Decimal("0.005"),
            max_risk_amount=Decimal("100"),
        )
    )
    assert result.volume == Decimal("0.10")


def test_a_non_positive_ceiling_is_refused() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("50"),
            stop_distance=Decimal("0.005"),
            max_risk_amount=Decimal("0"),
        )
    )
    assert result.refused


# ===================================================== 21. broker metadata


@pytest.mark.parametrize(
    "overrides",
    [
        {"volume_step": Decimal("0")},
        {"minimum_volume": Decimal("0")},
        {"maximum_volume": Decimal("0")},
        {"minimum_volume": Decimal("10"), "maximum_volume": Decimal("1")},
    ],
)
def test_unusable_broker_constraints_refuse(overrides: dict[str, Decimal]) -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_quantity, spec=spec(**overrides), quantity=Decimal("1")
        )
    )
    assert result.refused


def test_every_result_reports_the_venue_constraints_including_refusals() -> None:
    """A refusal that does not say what the limits were leaves the operator
    guessing whether the budget was too small or the instrument too coarse."""
    ok = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("0.005"),
        )
    )
    refused = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("1"),
            stop_distance=Decimal("0.005"),
        )
    )
    for result in (ok, refused):
        constraints = result.constraints or {}
        assert constraints["minimum_volume"] == "0.01"
        assert constraints["maximum_volume"] == "100"
        assert constraints["volume_step"] == "0.01"
        assert constraints["broker_symbol"] == "EURUSD"


# ======================================== 27-28. invalid and extreme numbers


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_number_is_refused_wherever_it_arrives(bad: str) -> None:
    """A NaN compares False against every bound, so an unchecked one walks
    through `<= 0`, through the limit checks, and out as a volume."""
    for field in ("risk_amount", "stop_distance", "entry_price", "stop_loss"):
        result = calculate(
            SizingRequest(
                method=SizingMethod.fixed_risk,
                spec=spec(),
                **{
                    "risk_amount": Decimal("100"),
                    "stop_distance": Decimal("0.005"),
                    field: Decimal(bad),
                },  # type: ignore[arg-type]
            )
        )
        assert result.refused, field
        assert result.volume is None


def test_a_non_finite_quantity_is_refused() -> None:
    result = calculate(
        SizingRequest(method=SizingMethod.fixed_quantity, spec=spec(), quantity=Decimal("NaN"))
    )
    assert result.refused


def test_an_extreme_budget_is_capped_by_the_venue_not_by_overflow() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("1e30"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert result.volume == Decimal("100")  # the venue maximum
    assert any("maximum" in w for w in result.warnings)


def test_an_extremely_tight_stop_does_not_produce_an_unbounded_size() -> None:
    result = calculate(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("1e-12"),
        )
    )
    assert result.ok
    assert result.volume is not None
    assert result.volume <= spec().maximum_volume


# ===================================================== determinism and shape


def test_the_engine_is_deterministic() -> None:
    """Same request, same result. No clock, no randomness, no I/O."""
    request = SizingRequest(
        method=SizingMethod.percent_equity,
        spec=spec(),
        equity=Decimal("10000"),
        risk_percent=Decimal("1"),
        side="buy",
        entry_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09500"),
    )
    first = calculate(request)
    for _ in range(20):
        assert calculate(request).as_dict() == first.as_dict()


def test_calculate_never_raises() -> None:
    """A malformed request is a refusal a bot loop can record, not an
    exception it has to classify mid-pass."""
    nonsense = SizingRequest(
        method=SizingMethod.fixed_risk,
        spec=spec(tick_size=None, tick_value=None),
        risk_amount=Decimal("NaN"),
        side="sideways",
        entry_price=Decimal("-1"),
        stop_loss=Decimal("0"),
    )
    result = calculate(nonsense)
    assert result.refused
    assert result.gap


# ======================================================= architecture fences


def test_sizing_holds_no_execution_authority() -> None:
    """The package may not import the broker layer, the paper engine or the
    Risk Engine. Sizing proposes a quantity; the authority to trade it lives
    elsewhere, and an import is how that boundary erodes first."""
    package = Path(__file__).resolve().parents[1] / "app" / "sizing"
    forbidden = ("app.brokers", "app.paper", "app.risk", "MetaTrader5", "subprocess")
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for bad in forbidden:
                    assert not name.startswith(bad), f"{path.name} imports {name}"


def test_the_engine_module_is_pure() -> None:
    """No clock and no randomness in the calculator, which is what makes
    `test_the_engine_is_deterministic` a property rather than a coincidence."""
    from app.sizing import calculator

    source = inspect.getsource(calculator)
    for banned in ("import random", "datetime.now", "time.time", "uuid4"):
        assert banned not in source, banned


def test_there_is_one_flooring_implementation() -> None:
    """The calculator floors through `app.symbols.precision`, which the broker
    layer and the OMS also use. Two functions that round a lot differently is
    how one signal sends two volumes depending on which path reached the
    venue."""
    from app.sizing import calculator

    source = inspect.getsource(calculator)
    assert "from app.symbols.precision import" in source
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_round_to_step" not in defined
    assert "floor_to_step" not in defined


# ============================================================== the service


def test_the_service_counts_successes_and_refusals() -> None:
    service = SizingService()
    service.size(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("0.005"),
        )
    )
    service.size(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(),
            risk_amount=Decimal("1"),
            stop_distance=Decimal("0.005"),
        )
    )
    status = service.status()
    assert status["position_sizing_requests_total"] == 2
    assert status["position_sizing_success_total"] == 1
    assert status["position_sizing_rejections_total"] == 1
    latency = status["quantity_calculation_latency_ms"]
    assert isinstance(latency, dict)
    assert latency["samples"] == 2


def test_the_service_counts_a_missing_spec_as_a_metadata_failure() -> None:
    service = SizingService()
    service.size(
        SizingRequest(
            method=SizingMethod.fixed_risk,
            spec=spec(tick_value=None),
            risk_amount=Decimal("100"),
            stop_distance=Decimal("0.005"),
        )
    )
    assert service.status()["broker_metadata_failures"] == 1


def test_the_latency_sample_is_bounded() -> None:
    """An unbounded list in a bot that runs for weeks is a leak."""
    service = SizingService()
    request = SizingRequest(
        method=SizingMethod.fixed_risk,
        spec=spec(),
        risk_amount=Decimal("100"),
        stop_distance=Decimal("0.005"),
    )
    for _ in range(600):
        service.size(request)
    latency = service.status()["quantity_calculation_latency_ms"]
    assert isinstance(latency, dict)
    assert latency["samples"] == 256
    assert service.status()["position_sizing_requests_total"] == 600
