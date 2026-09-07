"use client";

import { useState } from "react";
import { ApiError } from "@/lib/api";
import { sizingService, type SizingInput, type SizingResult } from "@/lib/services";
import { Badge, Button, Field, Input, Select } from "./ui";

/**
 * The position sizing panel (L18).
 *
 * **Every number on screen comes from the backend.** This component computes
 * nothing — not the stop distance, not the risk amount, not the quantity. A
 * size calculated in the browser is a second sizing implementation, and the
 * figure the operator reads would not be the figure an order would carry.
 * The inputs are collected, posted, and the answer is displayed verbatim.
 *
 * **A quantity is not a permission.** The panel shows the Risk Engine's
 * preview alongside the size, and says plainly that neither creates an order.
 */

const MODES = [
  { value: "percent_equity", label: "Percent of equity" },
  { value: "fixed_risk", label: "Fixed risk (account currency)" },
  { value: "fixed_quantity", label: "Fixed quantity" },
];

interface Form {
  symbol: string;
  side: "buy" | "sell";
  sizing_mode: string;
  entry_price: string;
  stop_loss: string;
  equity: string;
  risk_percent: string;
  risk_amount: string;
  quantity: string;
}

const BLANK: Form = {
  symbol: "EURUSD",
  side: "buy",
  sizing_mode: "percent_equity",
  entry_price: "",
  stop_loss: "",
  equity: "",
  risk_percent: "1",
  risk_amount: "",
  quantity: "",
};

function body(form: Form): SizingInput {
  const out: SizingInput = {
    symbol: form.symbol.trim().toUpperCase(),
    side: form.side,
    sizing_mode: form.sizing_mode,
  };
  // Only the fields the chosen mode uses are sent. An empty string is omitted
  // rather than sent as zero: "not stated" and "stated as nothing" are
  // different requests, and the backend refuses the second.
  const put = (key: keyof SizingInput, value: string) => {
    if (value.trim() !== "") (out as unknown as Record<string, string>)[key] = value.trim();
  };
  put("entry_price", form.entry_price);
  put("stop_loss", form.stop_loss);
  if (form.sizing_mode === "percent_equity") {
    put("equity", form.equity);
    put("risk_percent", form.risk_percent);
  } else if (form.sizing_mode === "fixed_risk") {
    put("risk_amount", form.risk_amount);
  } else {
    put("quantity", form.quantity);
  }
  return out;
}

export function SizingCalculator() {
  const [form, setForm] = useState<Form>(BLANK);
  const [result, setResult] = useState<SizingResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (key: keyof Form) => (value: string) => setForm((f) => ({ ...f, [key]: value }));

  async function calculate() {
    setBusy(true);
    setError(null);
    try {
      setResult(await sizingService.calculate(body(form)));
    } catch (err) {
      setResult(null);
      setError(err instanceof ApiError ? err.message : "the sizing request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Field label="Symbol">
          <Input
            value={form.symbol}
            onChange={(e) => set("symbol")(e.target.value)}
            aria-label="Symbol"
          />
        </Field>
        <Field label="Side">
          <Select
            value={form.side}
            onChange={(e) => set("side")(e.target.value)}
            aria-label="Side"
          >
            <option value="buy">Buy / long</option>
            <option value="sell">Sell / short</option>
          </Select>
        </Field>
      </div>

      <Field label="Sizing mode">
        <Select
          value={form.sizing_mode}
          onChange={(e) => set("sizing_mode")(e.target.value)}
          aria-label="Sizing mode"
        >
          {MODES.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </Select>
      </Field>

      <div className="grid grid-cols-2 gap-2">
        <Field label="Entry price">
          <Input
            value={form.entry_price}
            onChange={(e) => set("entry_price")(e.target.value)}
            inputMode="decimal"
            aria-label="Entry price"
          />
        </Field>
        <Field label="Stop loss">
          <Input
            value={form.stop_loss}
            onChange={(e) => set("stop_loss")(e.target.value)}
            inputMode="decimal"
            aria-label="Stop loss"
          />
        </Field>
      </div>

      {form.sizing_mode === "percent_equity" && (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Account equity">
            <Input
              value={form.equity}
              onChange={(e) => set("equity")(e.target.value)}
              inputMode="decimal"
              aria-label="Account equity"
            />
          </Field>
          <Field label="Risk %">
            <Input
              value={form.risk_percent}
              onChange={(e) => set("risk_percent")(e.target.value)}
              inputMode="decimal"
              aria-label="Risk percent"
            />
          </Field>
        </div>
      )}
      {form.sizing_mode === "fixed_risk" && (
        <Field label="Risk amount">
          <Input
            value={form.risk_amount}
            onChange={(e) => set("risk_amount")(e.target.value)}
            inputMode="decimal"
            aria-label="Risk amount"
          />
        </Field>
      )}
      {form.sizing_mode === "fixed_quantity" && (
        <Field label="Quantity">
          <Input
            value={form.quantity}
            onChange={(e) => set("quantity")(e.target.value)}
            inputMode="decimal"
            aria-label="Quantity"
          />
        </Field>
      )}

      <Button onClick={calculate} disabled={busy}>
        {busy ? "Calculating…" : "Calculate size"}
      </Button>

      {error && (
        <p role="alert" className="text-body text-critical">
          {error}
        </p>
      )}

      {result && (
        <div className="space-y-2 border-t border-line pt-2">
          <div className="flex items-center gap-2">
            <Badge tone={result.status === "VALID" ? "good" : "critical"}>{result.status}</Badge>
            <span className="font-mono text-micro text-muted">{result.sizing_mode}</span>
          </div>

          {result.status === "VALID" ? (
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-body">
              <Row label="Quantity" value={result.final_quantity} strong />
              <Row label="Raw quantity" value={result.raw_quantity} />
              <Row label="Stop distance" value={result.stop_distance} />
              <Row label="Risk per unit" value={result.risk_per_unit} />
              <Row label="Target risk" value={result.risk_amount} />
              <Row label="Actual risk" value={result.actual_risk} strong />
              <Row label="Broker minimum" value={result.broker_constraints?.minimum_volume} />
              <Row label="Broker maximum" value={result.broker_constraints?.maximum_volume} />
              <Row label="Quantity step" value={result.broker_constraints?.volume_step} />
              <Row label="Broker symbol" value={result.broker_constraints?.broker_symbol} />
            </dl>
          ) : (
            <p className="text-body text-ink-2">{result.gap}</p>
          )}

          {result.warnings.length > 0 && (
            <ul className="space-y-0.5 text-micro text-warning">
              {result.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}

          {result.risk?.outcome && (
            <p className="text-micro text-muted">
              Risk engine preview: <span className="font-mono">{result.risk.outcome}</span>
              {result.risk.reason ? ` — ${result.risk.reason}` : ""}
            </p>
          )}

          <p className="text-micro text-muted">{result.authority}</p>
        </div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  strong = false,
}: {
  label: string;
  value: string | null | undefined;
  strong?: boolean;
}) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className={`font-mono ${strong ? "text-ink" : "text-ink-2"}`}>
        {/* A missing figure reads as a dash, never as zero: "not computed" and
            "computed as nothing" are different facts. */}
        {value ?? "—"}
      </dd>
    </>
  );
}
