"""The declarative strategy definition, and the two validations it must pass.

A definition is **data**. It is parsed into frozen dataclasses, checked, and
then interpreted by a fixed evaluator in `app.strategies.built`. At no point is
any part of it turned into source, compiled, imported or executed. That is the
whole security model of the builder: a user can describe a comparison between
two indicators, and nothing else, and the set of things that can be described
is closed.

Two validations, and the second is the one that catches real mistakes.

**Structural** -- is this the right shape? Required fields present, types
right, no unknown keys, nesting within depth. Unknown keys are refused rather
than dropped, because a typo'd field that is silently ignored means the user is
running a strategy they did not write.

**Semantic** -- does it mean anything? The indicator exists, its parameters are
in range, the symbol is mapped, the timeframe is supported, and -- the
interesting one -- **the operands are comparable**. `PRICE > RSI` is refused
because a price level and a 0-100 oscillator are not the same quantity.
Comparing them produces a number that means nothing, which is the
metals-points error in miniature, and this project has already paid for that
class of mistake twice.

A definition that fails either validation cannot be saved as anything but a
draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.marketdata.types import Timeframe, TimeframeError, parse_timeframe
from app.strategies.base import SignalType
from app.strategies.indicators import PRICE_FIELDS, Unit, spec_for

# A rule tree deeper than this is unreadable in a UI and is almost always a
# mistake. Bounded so a hostile definition cannot exhaust the stack.
MAX_DEPTH = 6
MAX_CONDITIONS = 40
MAX_RULES = 10


class DefinitionError(Exception):
    """A definition that cannot be accepted. The message is safe to show."""


class Comparison(StrEnum):
    greater_than = "GREATER_THAN"
    less_than = "LESS_THAN"
    greater_or_equal = "GREATER_OR_EQUAL"
    less_or_equal = "LESS_OR_EQUAL"
    equal = "EQUAL"
    crosses_above = "CROSSES_ABOVE"
    crosses_below = "CROSSES_BELOW"

    @property
    def needs_previous_bar(self) -> bool:
        """A cross is defined by two bars, not one."""
        return self in (Comparison.crosses_above, Comparison.crosses_below)


class Logical(StrEnum):
    and_ = "AND"
    or_ = "OR"
    not_ = "NOT"


class OperandKind(StrEnum):
    indicator = "indicator"
    price = "price"
    constant = "constant"


# Which actions an entry rule and an exit rule may produce. Separated because
# an entry rule that could emit CLOSE would let the builder close positions it
# never opened.
ENTRY_ACTIONS = (SignalType.entry_long, SignalType.entry_short)
EXIT_ACTIONS = (SignalType.exit_long, SignalType.exit_short, SignalType.close)


@dataclass(frozen=True)
class Operand:
    kind: OperandKind
    # indicator: the catalogue key. price: a field name. constant: unused.
    ref: str = ""
    params: dict[str, int | float] = field(default_factory=dict)
    value: float | None = None

    @property
    def unit(self) -> Unit:
        if self.kind is OperandKind.indicator:
            return spec_for(self.ref).unit
        if self.kind is OperandKind.price:
            return PRICE_FIELDS[self.ref]
        return Unit.scalar

    def label(self) -> str:
        if self.kind is OperandKind.indicator:
            args = ", ".join(str(v) for v in self.params.values())
            return f"{self.ref}({args})" if args else self.ref
        if self.kind is OperandKind.price:
            return self.ref.upper()
        return str(self.value)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "ref": self.ref,
            "params": dict(self.params),
            "value": self.value,
        }


@dataclass(frozen=True)
class Condition:
    left: Operand
    comparison: Comparison
    right: Operand

    def label(self) -> str:
        words = {
            Comparison.greater_than: ">",
            Comparison.less_than: "<",
            Comparison.greater_or_equal: ">=",
            Comparison.less_or_equal: "<=",
            Comparison.equal: "==",
            Comparison.crosses_above: "crosses above",
            Comparison.crosses_below: "crosses below",
        }
        return f"{self.left.label()} {words[self.comparison]} {self.right.label()}"

    def as_dict(self) -> dict[str, object]:
        return {
            "type": "condition",
            "left": self.left.as_dict(),
            "comparison": str(self.comparison),
            "right": self.right.as_dict(),
        }


@dataclass(frozen=True)
class Group:
    logical: Logical
    children: tuple[Node, ...]

    def label(self) -> str:
        if self.logical is Logical.not_:
            return f"NOT ({self.children[0].label()})"
        joiner = f" {self.logical.value} "
        return "(" + joiner.join(c.label() for c in self.children) + ")"

    def as_dict(self) -> dict[str, object]:
        return {
            "type": "group",
            "logical": str(self.logical),
            "children": [c.as_dict() for c in self.children],
        }


Node = Condition | Group


@dataclass(frozen=True)
class Rule:
    when: Node
    then: SignalType

    def label(self) -> str:
        return f"WHEN {self.when.label()} THEN {self.then.value}"

    def as_dict(self) -> dict[str, object]:
        return {"when": self.when.as_dict(), "then": str(self.then)}


@dataclass(frozen=True)
class StrategyDefinition:
    """A whole strategy, as data."""

    name: str
    symbol: str
    timeframe: Timeframe
    entry_rules: tuple[Rule, ...]
    exit_rules: tuple[Rule, ...] = ()
    description: str = ""

    def indicators(self) -> list[Operand]:
        """Every distinct indicator operand the definition references."""
        found: dict[str, Operand] = {}
        for rule in (*self.entry_rules, *self.exit_rules):
            for operand in _operands(rule.when):
                if operand.kind is OperandKind.indicator:
                    found.setdefault(operand.label(), operand)
        return list(found.values())

    def warmup(self) -> int:
        """The longest warm-up any referenced indicator needs.

        A definition run on less data returns NO_SIGNAL rather than a signal
        computed from a half-filled indicator.
        """
        needed = [spec_for(o.ref).warmup(o.params) for o in self.indicators()]
        # A cross reads the previous bar, so at least two are always required.
        return max([*needed, 2])

    def summary(self) -> list[str]:
        """A readable account of the strategy, for the preview panel."""
        lines = [f"{self.name} — {self.symbol} {self.timeframe}"]
        for rule in self.entry_rules:
            lines.append(f"ENTRY: {rule.label()}")
        for rule in self.exit_rules:
            lines.append(f"EXIT:  {rule.label()}")
        if not self.exit_rules:
            lines.append(
                "EXIT:  none defined. Positions are closed by the stop, the target or "
                "the position manager, not by this strategy."
            )
        return lines

    def to_payload(self) -> dict[str, object]:
        """The canonical, **round-trippable** form.

        Only the fields `parse_definition` accepts, so a stored definition can
        be read back and re-parsed. `as_dict` adds derived views for the API
        and is deliberately NOT what gets persisted -- storing the enriched
        shape made a saved version unparseable, which is how a strategy becomes
        unreadable to the platform that wrote it.
        """
        return {
            "name": self.name,
            "description": self.description,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "entry_rules": [r.as_dict() for r in self.entry_rules],
            "exit_rules": [r.as_dict() for r in self.exit_rules],
        }

    def as_dict(self) -> dict[str, object]:
        """The payload plus derived views, for an API response."""
        return {
            **self.to_payload(),
            "indicators": [o.as_dict() for o in self.indicators()],
            "warmup_bars": self.warmup(),
            "summary": self.summary(),
        }


def _operands(node: Node) -> list[Operand]:
    if isinstance(node, Condition):
        return [node.left, node.right]
    out: list[Operand] = []
    for child in node.children:
        out.extend(_operands(child))
    return out


# ---------------------------------------------------------- structural


def _require(payload: object, where: str) -> dict:
    if not isinstance(payload, dict):
        raise DefinitionError(f"{where} must be an object, not {type(payload).__name__}")
    return payload


def _no_unknown(payload: dict, allowed: set[str], where: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        # Refused, never dropped: a silently ignored field means the user is
        # running a strategy they did not write.
        raise DefinitionError(
            f"{where} has unknown fields {sorted(unknown)}; allowed: {sorted(allowed)}"
        )


def _string(payload: dict, key: str, where: str, *, max_length: int = 200) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{where}.{key} is required and must be a non-empty string")
    if len(value) > max_length:
        raise DefinitionError(f"{where}.{key} is longer than {max_length} characters")
    return value.strip()


def parse_operand(payload: object, where: str) -> Operand:
    data = _require(payload, where)
    _no_unknown(data, {"kind", "ref", "params", "value"}, where)
    raw_kind = data.get("kind")
    try:
        kind = OperandKind(str(raw_kind))
    except ValueError as exc:
        raise DefinitionError(
            f"{where}.kind must be one of {', '.join(str(k) for k in OperandKind)}, "
            f"not {raw_kind!r}"
        ) from exc

    if kind is OperandKind.constant:
        value = data.get("value")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise DefinitionError(f"{where}.value must be a number for a constant operand")
        return Operand(kind, value=float(value))

    ref = data.get("ref")
    if not isinstance(ref, str) or not ref:
        raise DefinitionError(f"{where}.ref is required for a {kind} operand")

    if kind is OperandKind.price:
        if ref not in PRICE_FIELDS:
            raise DefinitionError(
                f"{where}.ref {ref!r} is not a price field; available: "
                f"{', '.join(sorted(PRICE_FIELDS))}"
            )
        return Operand(kind, ref=ref)

    # Indicator. `spec_for` refuses an unknown key and `validate_params`
    # refuses an out-of-range period, so both are semantic checks that happen
    # here because the data to make them is right here.
    try:
        spec = spec_for(ref)
        params = spec.validate_params(data.get("params") or {})
    except ValueError as exc:
        raise DefinitionError(f"{where}: {exc}") from exc
    return Operand(kind, ref=ref, params=params)


def parse_node(payload: object, where: str, depth: int = 0) -> Node:
    if depth > MAX_DEPTH:
        raise DefinitionError(
            f"{where} nests deeper than {MAX_DEPTH} levels; a tree that deep is "
            "unreadable and almost always a mistake"
        )
    data = _require(payload, where)
    kind = data.get("type")
    if kind == "condition":
        _no_unknown(data, {"type", "left", "comparison", "right"}, where)
        raw = data.get("comparison")
        try:
            comparison = Comparison(str(raw))
        except ValueError as exc:
            raise DefinitionError(
                f"{where}.comparison must be one of "
                f"{', '.join(str(c) for c in Comparison)}, not {raw!r}"
            ) from exc
        left = parse_operand(data.get("left"), f"{where}.left")
        right = parse_operand(data.get("right"), f"{where}.right")
        _check_comparable(left, right, comparison, where)
        return Condition(left, comparison, right)

    if kind == "group":
        _no_unknown(data, {"type", "logical", "children"}, where)
        raw = data.get("logical")
        try:
            logical = Logical(str(raw))
        except ValueError as exc:
            raise DefinitionError(f"{where}.logical must be AND, OR or NOT, not {raw!r}") from exc
        children = data.get("children")
        if not isinstance(children, list) or not children:
            raise DefinitionError(f"{where}.children must be a non-empty list")
        if logical is Logical.not_ and len(children) != 1:
            raise DefinitionError(f"{where}: NOT takes exactly one child, not {len(children)}")
        if logical is not Logical.not_ and len(children) < 2:
            raise DefinitionError(f"{where}: {logical} needs at least two children")
        parsed = tuple(
            parse_node(child, f"{where}.children[{i}]", depth + 1)
            for i, child in enumerate(children)
        )
        return Group(logical, parsed)

    raise DefinitionError(f"{where}.type must be 'condition' or 'group', not {kind!r}")


def _check_comparable(left: Operand, right: Operand, comparison: Comparison, where: str) -> None:
    """Refuse a comparison between quantities that are not the same kind.

    `PRICE > RSI` is the example the prompt gives and it is exactly right: a
    price level and a 0-100 oscillator are different quantities, and the
    comparison produces a number that means nothing. A constant is compatible
    with anything, because it takes its meaning from what it is compared
    against.
    """
    if left.kind is OperandKind.constant and right.kind is OperandKind.constant:
        raise DefinitionError(
            f"{where} compares two constants, which is a fixed answer rather than a condition"
        )
    left_unit, right_unit = left.unit, right.unit
    if Unit.scalar in (left_unit, right_unit):
        return
    if left_unit is not right_unit:
        raise DefinitionError(
            f"{where}: cannot compare {left.label()} ({left_unit}) with "
            f"{right.label()} ({right_unit}). They are different quantities, and a "
            "comparison between them means nothing"
        )
    if comparison.needs_previous_bar and (
        left.kind is OperandKind.constant and right.kind is OperandKind.constant
    ):  # pragma: no cover - caught above
        raise DefinitionError(f"{where}: a cross between two constants never happens")


def parse_rule(payload: object, where: str, *, allowed: tuple[SignalType, ...]) -> Rule:
    data = _require(payload, where)
    _no_unknown(data, {"when", "then"}, where)
    raw = data.get("then")
    try:
        action = SignalType(str(raw))
    except ValueError as exc:
        raise DefinitionError(
            f"{where}.then must be one of {', '.join(a.value for a in allowed)}, not {raw!r}"
        ) from exc
    if action not in allowed:
        raise DefinitionError(
            f"{where}.then may not be {action.value} here; allowed: "
            f"{', '.join(a.value for a in allowed)}"
        )
    return Rule(parse_node(data.get("when"), f"{where}.when"), action)


def parse_definition(payload: object) -> StrategyDefinition:
    """Structural + operand-level semantic validation. Raises `DefinitionError`.

    Symbol and timeframe existence is checked separately by
    `validate_against_platform`, which needs a database session; everything
    that can be decided from the payload alone is decided here.
    """
    data = _require(payload, "definition")
    _no_unknown(
        data,
        {"name", "description", "symbol", "timeframe", "entry_rules", "exit_rules"},
        "definition",
    )
    name = _string(data, "name", "definition", max_length=120)
    symbol = _string(data, "symbol", "definition", max_length=32).upper()
    description = data.get("description") or ""
    if not isinstance(description, str) or len(description) > 1000:
        raise DefinitionError("definition.description must be a string of at most 1000 characters")

    try:
        timeframe = parse_timeframe(_string(data, "timeframe", "definition", max_length=8))
    except TimeframeError as exc:
        raise DefinitionError(str(exc)) from exc

    entries = data.get("entry_rules")
    if not isinstance(entries, list) or not entries:
        raise DefinitionError(
            "definition.entry_rules must contain at least one rule; a strategy with no "
            "entry rule can never do anything"
        )
    if len(entries) > MAX_RULES:
        raise DefinitionError(f"at most {MAX_RULES} entry rules")
    exits = data.get("exit_rules") or []
    if not isinstance(exits, list):
        raise DefinitionError("definition.exit_rules must be a list")
    if len(exits) > MAX_RULES:
        raise DefinitionError(f"at most {MAX_RULES} exit rules")

    entry_rules = tuple(
        parse_rule(r, f"entry_rules[{i}]", allowed=ENTRY_ACTIONS) for i, r in enumerate(entries)
    )
    exit_rules = tuple(
        parse_rule(r, f"exit_rules[{i}]", allowed=EXIT_ACTIONS) for i, r in enumerate(exits)
    )

    total = sum(_count_conditions(r.when) for r in (*entry_rules, *exit_rules))
    if total > MAX_CONDITIONS:
        raise DefinitionError(f"at most {MAX_CONDITIONS} conditions in one strategy, not {total}")

    return StrategyDefinition(
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        entry_rules=entry_rules,
        exit_rules=exit_rules,
        description=description,
    )


def _count_conditions(node: Node) -> int:
    if isinstance(node, Condition):
        return 1
    return sum(_count_conditions(c) for c in node.children)


async def validate_against_platform(db: Any, definition: StrategyDefinition) -> list[str]:
    """The semantic checks that need the platform: does this symbol exist?

    Returns a list of problems; empty means it passes. Separated from
    `parse_definition` so a definition can be checked offline -- in a test, in
    the builder's live preview -- without a database.
    """
    from app.symbols import service as symbols
    from app.symbols.errors import SymbolError

    problems: list[str] = []
    try:
        await symbols.get_symbol(db, definition.symbol)
    except SymbolError as exc:
        # Never invents a broker symbol. An unmapped instrument is a refusal.
        problems.append(f"symbol: {exc}")
    return problems
