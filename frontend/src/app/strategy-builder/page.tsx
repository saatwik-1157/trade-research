"use client";

/**
 * The visual strategy builder.
 *
 * A structured form editor rather than drag-and-drop, deliberately. Step 13 of
 * the brief says not to introduce drag-and-drop complexity where a simpler and
 * more reliable editor will do, and correctness matters more than visual
 * complexity here: every control below maps to exactly one field of a
 * validated definition, and there is no arrangement of them that produces
 * something the server will not accept for a reason the user can read.
 *
 * Three things this screen is careful about:
 *
 *  - **The server validates.** The panel on the right shows the server's
 *    answer, not the browser's. The only client-side check is the unit
 *    compatibility hint, and it is a hint.
 *  - **Nothing pretends.** The backtest button says NOT RUN, because the
 *    backtest runner is L14 and showing a plausible number would be worse
 *    than showing nothing.
 *  - **A saved strategy is research-only.** Nothing about building a strategy
 *    is evidence that it works, and the screen says so where it would
 *    otherwise be tempting to assume.
 */

import { useCallback, useEffect, useState } from "react";
import { PageHeader, Panel, Badge, Button, Field, Input, Select } from "@/components/ui";
import { ApiError } from "@/lib/api";
import {
  type BuilderCatalogue,
  type ConditionNode,
  type Operand,
  type Rule,
  type RuleNode,
  type StrategyDefinition,
  type ValidationResult,
  builderApi,
  describeNode,
  emptyDefinition,
  unitsCompatible,
} from "@/lib/builder";

const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"];

function isCondition(node: RuleNode): node is ConditionNode {
  return node.type === "condition";
}

/** One operand: what kind, which indicator or field, and its period. */
function OperandEditor({
  operand,
  catalogue,
  onChange,
  label,
}: {
  operand: Operand;
  catalogue: BuilderCatalogue | null;
  onChange: (next: Operand) => void;
  label: string;
}) {
  const spec = catalogue?.indicators.find((i) => i.key === operand.ref);
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Field label={label}>
        <Select
          value={operand.kind}
          onChange={(e) => {
            const kind = e.target.value as Operand["kind"];
            if (kind === "indicator") onChange({ kind, ref: "EMA", params: { period: 20 } });
            else if (kind === "price") onChange({ kind, ref: "close" });
            else onChange({ kind, value: 0 });
          }}
        >
          {(catalogue?.operand_kinds ?? ["indicator", "price", "constant"]).map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </Select>
      </Field>

      {operand.kind === "indicator" && (
        <>
          <Field label="indicator">
            <Select
              value={operand.ref ?? ""}
              onChange={(e) => {
                const key = e.target.value;
                const next = catalogue?.indicators.find((i) => i.key === key);
                const params: Record<string, number> = {};
                for (const p of next?.parameters ?? []) params[p.name] = p.default;
                onChange({ kind: "indicator", ref: key, params });
              }}
            >
              {(catalogue?.indicators ?? []).map((i) => (
                <option key={i.key} value={i.key}>
                  {i.key} — {i.unit}
                </option>
              ))}
            </Select>
          </Field>
          {(spec?.parameters ?? []).map((p) => (
            <Field key={p.name} label={p.name}>
              <Input
                type="number"
                min={p.minimum}
                max={p.maximum}
                step={p.step}
                value={operand.params?.[p.name] ?? p.default}
                onChange={(e) =>
                  onChange({
                    ...operand,
                    params: { ...operand.params, [p.name]: Number(e.target.value) },
                  })
                }
              />
            </Field>
          ))}
        </>
      )}

      {operand.kind === "price" && (
        <Field label="field">
          <Select
            value={operand.ref ?? "close"}
            onChange={(e) => onChange({ kind: "price", ref: e.target.value })}
          >
            {(catalogue?.price_fields ?? []).map((f) => (
              <option key={f.key} value={f.key}>
                {f.key}
              </option>
            ))}
          </Select>
        </Field>
      )}

      {operand.kind === "constant" && (
        <Field label="value">
          <Input
            type="number"
            step="any"
            value={operand.value ?? 0}
            onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })}
          />
        </Field>
      )}
    </div>
  );
}

/** One condition, plus the incompatibility hint. */
function ConditionEditor({
  node,
  catalogue,
  onChange,
}: {
  node: ConditionNode;
  catalogue: BuilderCatalogue | null;
  onChange: (next: ConditionNode) => void;
}) {
  const compatible = unitsCompatible(node.left, node.right, catalogue);
  return (
    <div className="space-y-2 rounded border border-line p-3">
      <OperandEditor
        label="left"
        operand={node.left}
        catalogue={catalogue}
        onChange={(left) => onChange({ ...node, left })}
      />
      <Field label="comparison">
        <Select
          value={node.comparison}
          onChange={(e) =>
            onChange({ ...node, comparison: e.target.value as ConditionNode["comparison"] })
          }
        >
          {(catalogue?.comparisons ?? []).map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </Select>
      </Field>
      <OperandEditor
        label="right"
        operand={node.right}
        catalogue={catalogue}
        onChange={(right) => onChange({ ...node, right })}
      />
      {!compatible && (
        <p className="text-body text-warning">
          These are different quantities, so comparing them means nothing. The server will
          refuse this condition.
        </p>
      )}
    </div>
  );
}

export default function Page() {
  const [catalogue, setCatalogue] = useState<BuilderCatalogue | null>(null);
  const [catalogueError, setCatalogueError] = useState<string | null>(null);
  const [definition, setDefinition] = useState<StrategyDefinition>(emptyDefinition);
  const [key, setKey] = useState("");
  const [result, setResult] = useState<ValidationResult | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    builderApi
      .catalogue()
      .then(setCatalogue)
      .catch((e: unknown) =>
        setCatalogueError(e instanceof ApiError ? e.message : "could not load the catalogue"),
      );
  }, []);

  const validate = useCallback(async () => {
    setBusy(true);
    setSaveMessage(null);
    try {
      setResult(await builderApi.validate(definition));
    } catch (e: unknown) {
      // The server's message names the exact field. It is shown as-is.
      setResult({
        valid: false,
        problems: [e instanceof ApiError ? e.message : "validation failed"],
        summary: [],
        warmup_bars: 0,
        indicators: [],
        note: "",
      });
    } finally {
      setBusy(false);
    }
  }, [definition]);

  const save = useCallback(async () => {
    setBusy(true);
    try {
      const created = await builderApi.create(key, definition);
      setSaveMessage(
        `Saved ${created.key} v${created.version} as ${created.status}. It is research-only: nothing has measured it.`,
      );
    } catch (e: unknown) {
      setSaveMessage(e instanceof ApiError ? e.message : "could not save");
    } finally {
      setBusy(false);
    }
  }, [key, definition]);

  const setRule = (list: "entry_rules" | "exit_rules", index: number, rule: Rule) =>
    setDefinition((d) => ({
      ...d,
      [list]: d[list].map((r, i) => (i === index ? rule : r)),
    }));

  const addRule = (list: "entry_rules" | "exit_rules") =>
    setDefinition((d) => ({
      ...d,
      [list]: [
        ...d[list],
        {
          when: {
            type: "condition",
            left: { kind: "indicator", ref: "RSI", params: { period: 14 } },
            comparison: list === "entry_rules" ? "GREATER_THAN" : "LESS_THAN",
            right: { kind: "constant", value: list === "entry_rules" ? 50 : 40 },
          },
          then: list === "entry_rules" ? "ENTRY_LONG" : "CLOSE",
        } as Rule,
      ],
    }));

  const removeRule = (list: "entry_rules" | "exit_rules", index: number) =>
    setDefinition((d) => ({ ...d, [list]: d[list].filter((_, i) => i !== index) }));

  const ruleSection = (list: "entry_rules" | "exit_rules", title: string) => (
    <Panel title={title}>
      <div className="space-y-3">
        {definition[list].map((rule, index) => (
          <div key={index} className="space-y-2 rounded border border-line p-3">
            <div className="flex items-center justify-between">
              <span className="text-mini uppercase tracking-wide text-muted">
                WHEN
              </span>
              <Button variant="ghost" onClick={() => removeRule(list, index)}>
                Remove
              </Button>
            </div>
            {isCondition(rule.when) ? (
              <ConditionEditor
                node={rule.when}
                catalogue={catalogue}
                onChange={(when) => setRule(list, index, { ...rule, when })}
              />
            ) : (
              <p className="text-body text-muted">
                {describeNode(rule.when)} — nested groups are edited in the definition view.
              </p>
            )}
            <Field label="THEN">
              <Select
                value={rule.then}
                onChange={(e) =>
                  setRule(list, index, { ...rule, then: e.target.value as Rule["then"] })
                }
              >
                {(list === "entry_rules"
                  ? (catalogue?.entry_actions ?? ["ENTRY_LONG", "ENTRY_SHORT"])
                  : (catalogue?.exit_actions ?? ["EXIT_LONG", "EXIT_SHORT", "CLOSE"])
                ).map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        ))}
        <Button variant="secondary" onClick={() => addRule(list)}>
          Add rule
        </Button>
        {list === "exit_rules" && definition.exit_rules.length === 0 && (
          <p className="text-body text-muted">
            No exit rule. Positions are closed by the stop, the target or the position
            manager, not by this strategy.
          </p>
        )}
      </div>
    </Panel>
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Strategy Builder"
        level={13}
        description="Build a strategy as validated data. Nothing here becomes code, and a strategy produces a signal, never an order."
      />

      {catalogueError && (
        <p className="text-sm text-critical">
          The indicator catalogue could not be loaded, so the editor is showing defaults:{" "}
          {catalogueError}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Panel title="Strategy">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Key (lowercase, digits, underscore)">
                <Input
                  value={key}
                  placeholder="my_ema_cross"
                  onChange={(e) => setKey(e.target.value)}
                />
              </Field>
              <Field label="Name">
                <Input
                  value={definition.name}
                  onChange={(e) => setDefinition({ ...definition, name: e.target.value })}
                />
              </Field>
              <Field label="Symbol (internal code)">
                <Input
                  value={definition.symbol}
                  onChange={(e) =>
                    setDefinition({ ...definition, symbol: e.target.value.toUpperCase() })
                  }
                />
              </Field>
              <Field label="Timeframe">
                <Select
                  value={definition.timeframe}
                  onChange={(e) =>
                    setDefinition({ ...definition, timeframe: e.target.value })
                  }
                >
                  {TIMEFRAMES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div className="mt-3">
              <Field label="Description">
                <Input
                  value={definition.description ?? ""}
                  onChange={(e) =>
                    setDefinition({ ...definition, description: e.target.value })
                  }
                />
              </Field>
            </div>
          </Panel>

          {ruleSection("entry_rules", "Entry rules")}
          {ruleSection("exit_rules", "Exit rules")}
        </div>

        <div className="space-y-4">
          <Panel title="Preview">
            <ul className="space-y-1 font-mono text-body text-ink-2">
              {definition.entry_rules.map((r, i) => (
                <li key={`e${i}`}>ENTRY: {describeNode(r.when)} → {r.then}</li>
              ))}
              {definition.exit_rules.map((r, i) => (
                <li key={`x${i}`}>EXIT: {describeNode(r.when)} → {r.then}</li>
              ))}
            </ul>
          </Panel>

          <Panel title="Validation">
            <div className="space-y-2">
              <Button onClick={validate} disabled={busy}>
                {busy ? "Checking…" : "Validate"}
              </Button>
              {result === null ? (
                <p className="text-body text-muted">Not checked yet.</p>
              ) : (
                <>
                  <Badge tone={result.valid ? "good" : "critical"}>
                    {result.valid ? "VALID" : "INVALID"}
                  </Badge>
                  {result.problems.map((p, i) => (
                    <p key={i} className="text-body text-critical">
                      {p}
                    </p>
                  ))}
                  {result.valid && (
                    <>
                      <p className="text-body text-muted">
                        Warm-up: {result.warmup_bars} bars before the longest indicator
                        means anything.
                      </p>
                      <p className="text-body text-muted">{result.note}</p>
                    </>
                  )}
                </>
              )}
            </div>
          </Panel>

          <Panel title="Save">
            <div className="space-y-2">
              <Button onClick={save} disabled={busy || !key || !definition.name}>
                Save draft
              </Button>
              {saveMessage && <p className="text-body text-ink-2">{saveMessage}</p>}
              <p className="text-body text-muted">
                A saved strategy is research-only. Building one is not evidence that it
                works.
              </p>
            </div>
          </Panel>

          <Panel title="Backtest">
            {/* Never a plausible-looking number. The runner is L14. */}
            <Badge tone="neutral">NOT RUN</Badge>
            <p className="mt-2 text-body text-muted">
              The backtesting engine is built at level 14. No result is shown because none
              exists.
            </p>
          </Panel>

          <Panel title="Definition (read-only)">
            <pre className="max-h-64 overflow-auto text-micro leading-relaxed text-muted">
              {JSON.stringify(definition, null, 2)}
            </pre>
          </Panel>
        </div>
      </div>
    </div>
  );
}
