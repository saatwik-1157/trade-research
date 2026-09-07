import { describe, expect, it } from "vitest";
import {
  type BuilderCatalogue,
  type Operand,
  describeNode,
  describeOperand,
  emptyDefinition,
  unitsCompatible,
} from "./builder";

const catalogue: BuilderCatalogue = {
  indicators: [
    { key: "EMA", name: "EMA", unit: "price", description: "", parameters: [] },
    { key: "RSI", name: "RSI", unit: "oscillator", description: "", parameters: [] },
    { key: "ATR", name: "ATR", unit: "volatility", description: "", parameters: [] },
  ],
  price_fields: [
    { key: "close", unit: "price" },
    { key: "volume", unit: "volume" },
  ],
  comparisons: ["GREATER_THAN", "CROSSES_ABOVE"],
  logical: ["AND", "OR", "NOT"],
  operand_kinds: ["indicator", "price", "constant"],
  entry_actions: ["ENTRY_LONG", "ENTRY_SHORT"],
  exit_actions: ["EXIT_LONG", "EXIT_SHORT", "CLOSE"],
  note: "",
};

const ema = (period: number): Operand => ({
  kind: "indicator",
  ref: "EMA",
  params: { period },
});
const rsi: Operand = { kind: "indicator", ref: "RSI", params: { period: 14 } };
const close: Operand = { kind: "price", ref: "close" };
const fifty: Operand = { kind: "constant", value: 50 };

describe("readable descriptions", () => {
  it("renders an indicator with its parameters", () => {
    expect(describeOperand(ema(20))).toBe("EMA(20)");
  });

  it("renders a price field and a constant", () => {
    expect(describeOperand(close)).toBe("CLOSE");
    expect(describeOperand(fifty)).toBe("50");
  });

  it("renders a condition the way a person would read it", () => {
    expect(
      describeNode({
        type: "condition",
        left: ema(20),
        comparison: "CROSSES_ABOVE",
        right: ema(50),
      }),
    ).toBe("EMA(20) crosses above EMA(50)");
  });

  it("renders nested groups with their operator", () => {
    const text = describeNode({
      type: "group",
      logical: "AND",
      children: [
        { type: "condition", left: ema(20), comparison: "GREATER_THAN", right: ema(50) },
        { type: "condition", left: rsi, comparison: "GREATER_THAN", right: fifty },
      ],
    });
    expect(text).toBe("(EMA(20) > EMA(50) AND RSI(14) > 50)");
  });

  it("renders NOT", () => {
    const text = describeNode({
      type: "group",
      logical: "NOT",
      children: [
        { type: "condition", left: rsi, comparison: "GREATER_THAN", right: fifty },
      ],
    });
    expect(text).toBe("NOT (RSI(14) > 50)");
  });
});

describe("unit compatibility", () => {
  it("refuses a price against an oscillator", () => {
    // The example the brief gives: they are different quantities and the
    // comparison means nothing. The server makes the same check.
    expect(unitsCompatible(close, rsi, catalogue)).toBe(false);
  });

  it("refuses a volatility measure against a price level", () => {
    const atr: Operand = { kind: "indicator", ref: "ATR", params: { period: 14 } };
    expect(unitsCompatible(atr, close, catalogue)).toBe(false);
  });

  it("accepts two operands of the same unit", () => {
    expect(unitsCompatible(ema(20), ema(50), catalogue)).toBe(true);
    expect(unitsCompatible(close, ema(20), catalogue)).toBe(true);
  });

  it("accepts a constant against anything, because it takes its meaning from the comparison", () => {
    expect(unitsCompatible(rsi, fifty, catalogue)).toBe(true);
    expect(unitsCompatible(close, fifty, catalogue)).toBe(true);
  });

  it("does not block while the catalogue is still loading", () => {
    // A hint that fired before the data arrived would be a false warning.
    expect(unitsCompatible(close, rsi, null)).toBe(true);
  });
});

describe("the starting definition", () => {
  it("is something the server will accept", () => {
    const definition = emptyDefinition();
    // At least one entry rule: a strategy with none can never do anything, and
    // starting emptier would show a validation error before the user acted.
    expect(definition.entry_rules).toHaveLength(1);
    expect(definition.exit_rules).toHaveLength(0);
    expect(definition.timeframe).toBe("H1");
  });
});
