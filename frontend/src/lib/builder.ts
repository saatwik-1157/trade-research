/**
 * The strategy-definition types the builder edits, and the calls it makes.
 *
 * These mirror `app/strategies/definition.py`. The **server** is the
 * authority: every definition is re-validated there before it is stored, and
 * the client's checks exist to give fast feedback, never to be trusted. A
 * builder that validated only in the browser would be a builder anyone could
 * bypass with curl.
 *
 * A definition is data. Nothing here generates code, and the backend never
 * executes one — it walks it with a fixed evaluator.
 */
import { request } from "./api";

export type Comparison =
  | "GREATER_THAN"
  | "LESS_THAN"
  | "GREATER_OR_EQUAL"
  | "LESS_OR_EQUAL"
  | "EQUAL"
  | "CROSSES_ABOVE"
  | "CROSSES_BELOW";

export type Logical = "AND" | "OR" | "NOT";
export type OperandKind = "indicator" | "price" | "constant";
export type EntryAction = "ENTRY_LONG" | "ENTRY_SHORT";
export type ExitAction = "EXIT_LONG" | "EXIT_SHORT" | "CLOSE";

export interface Operand {
  kind: OperandKind;
  ref?: string;
  params?: Record<string, number>;
  value?: number;
}

export interface ConditionNode {
  type: "condition";
  left: Operand;
  comparison: Comparison;
  right: Operand;
}

export interface GroupNode {
  type: "group";
  logical: Logical;
  children: RuleNode[];
}

export type RuleNode = ConditionNode | GroupNode;

export interface Rule {
  when: RuleNode;
  then: EntryAction | ExitAction;
}

export interface StrategyDefinition {
  name: string;
  description?: string;
  symbol: string;
  timeframe: string;
  entry_rules: Rule[];
  exit_rules: Rule[];
}

export interface IndicatorParameter {
  name: string;
  kind: "int" | "float";
  default: number;
  minimum: number;
  maximum: number;
  step: number;
  description: string;
}

export interface IndicatorSpec {
  key: string;
  name: string;
  /** What makes `PRICE > RSI` refusable: they are different quantities. */
  unit: string;
  description: string;
  parameters: IndicatorParameter[];
}

export interface BuilderCatalogue {
  indicators: IndicatorSpec[];
  price_fields: { key: string; unit: string }[];
  comparisons: Comparison[];
  logical: Logical[];
  operand_kinds: OperandKind[];
  entry_actions: EntryAction[];
  exit_actions: ExitAction[];
  note: string;
}

export interface ValidationResult {
  valid: boolean;
  problems: string[];
  summary: string[];
  warmup_bars: number;
  indicators: string[];
  note: string;
}

/** A blank strategy: one entry rule with one condition, which is the minimum
 * the server will accept. Starting emptier would show the user a validation
 * error before they had done anything. */
export function emptyDefinition(): StrategyDefinition {
  return {
    name: "",
    description: "",
    symbol: "EURUSD",
    timeframe: "H1",
    entry_rules: [
      {
        when: {
          type: "condition",
          left: { kind: "indicator", ref: "EMA", params: { period: 20 } },
          comparison: "CROSSES_ABOVE",
          right: { kind: "indicator", ref: "EMA", params: { period: 50 } },
        },
        then: "ENTRY_LONG",
      },
    ],
    exit_rules: [],
  };
}

export function describeOperand(operand: Operand): string {
  if (operand.kind === "constant") return String(operand.value ?? "");
  if (operand.kind === "price") return (operand.ref ?? "").toUpperCase();
  const args = Object.values(operand.params ?? {}).join(", ");
  return args ? `${operand.ref}(${args})` : (operand.ref ?? "");
}

const SYMBOLS: Record<Comparison, string> = {
  GREATER_THAN: ">",
  LESS_THAN: "<",
  GREATER_OR_EQUAL: ">=",
  LESS_OR_EQUAL: "<=",
  EQUAL: "==",
  CROSSES_ABOVE: "crosses above",
  CROSSES_BELOW: "crosses below",
};

export function describeNode(node: RuleNode): string {
  if (node.type === "condition") {
    return `${describeOperand(node.left)} ${SYMBOLS[node.comparison]} ${describeOperand(node.right)}`;
  }
  if (node.logical === "NOT") return `NOT (${describeNode(node.children[0])})`;
  return `(${node.children.map(describeNode).join(` ${node.logical} `)})`;
}

/**
 * Client-side compatibility check, for immediate feedback only.
 *
 * A constant is compatible with anything because it takes its meaning from
 * what it is compared against. Two operands with different units are not:
 * a price level and a 0-100 oscillator are different quantities. **The server
 * makes the same check and its answer is the one that counts.**
 */
export function unitsCompatible(
  left: Operand,
  right: Operand,
  catalogue: BuilderCatalogue | null,
): boolean {
  if (!catalogue) return true;
  const unitOf = (operand: Operand): string | null => {
    if (operand.kind === "constant") return null;
    if (operand.kind === "price") {
      return catalogue.price_fields.find((f) => f.key === operand.ref)?.unit ?? null;
    }
    return catalogue.indicators.find((i) => i.key === operand.ref)?.unit ?? null;
  };
  const a = unitOf(left);
  const b = unitOf(right);
  if (a === null || b === null) return true;
  return a === b;
}

export const builderApi = {
  catalogue: (): Promise<BuilderCatalogue> =>
    request<BuilderCatalogue>("/v1/strategy-builder/catalogue"),

  validate: (definition: StrategyDefinition): Promise<ValidationResult> =>
    request<ValidationResult>("/v1/strategy-builder/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ definition }),
    }),

  create: (key: string, definition: StrategyDefinition) =>
    request<{ key: string; version: number; status: string; problems: string[] }>(
      "/v1/strategy-builder",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, definition }),
      },
    ),

  list: () =>
    request<{ strategies: { key: string; name: string; latest_version: number | null; latest_status: string | null; versions: number }[]; count: number }>(
      "/v1/strategy-builder",
    ),
};
