"use client";

import { useMemo, useState } from "react";
import { Button, ConfirmDialog, Field, Input, Select } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { orderService, type OrderRow } from "@/lib/services";
import type { Health } from "@/lib/types";

const TYPES = ["market", "limit", "stop"] as const;

/**
 * The order ticket. Bound to the real submission path at L19.
 *
 * **The browser computes nothing that matters.** The risk/reward figures here
 * are a convenience read-out of what the operator typed; the quantity that
 * gets traded is decided by the backend's sizing engine, and the answer shown
 * after a submit is the order the backend created, never an optimistic guess.
 *
 * **A click can never read as a fill.** The status shown is whatever state
 * the OMS reports — `submitting`, `unknown` and `failed` included. Nothing
 * here renders "filled" unless the venue said so.
 *
 * **One click is one order.** Each submission carries a fresh idempotency
 * key, and a retry of the same attempt reuses it, so a double click cannot
 * become two orders. The key is generated once per attempt rather than per
 * render for exactly that reason.
 */
export function OrderPanel({ symbol, health }: { symbol: string; health?: Health }) {
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [type, setType] = useState<(typeof TYPES)[number]>("market");
  const [quantity, setQuantity] = useState("0.10");
  const [entry, setEntry] = useState("");
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const [account, setAccount] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [placed, setPlaced] = useState<OrderRow | null>(null);
  const [busy, setBusy] = useState(false);

  const risk = useMemo(() => {
    const q = Number(quantity);
    const e = Number(entry);
    const s = Number(stop);
    const t = Number(target);
    if (!q || !e || !s) return null;
    const riskDistance = Math.abs(e - s);
    const rewardDistance = t ? Math.abs(t - e) : null;
    return {
      riskDistance,
      rewardDistance,
      ratio: rewardDistance ? rewardDistance / riskDistance : null,
      estimated: riskDistance * q,
    };
  }, [quantity, entry, stop, target]);

  async function onConfirm() {
    setConfirming(false);
    setBusy(true);
    setOutcome(null);
    setPlaced(null);
    try {
      // One key per attempt. Generated here rather than in render, so a
      // re-render cannot mint a second identity for the same click.
      const key = crypto.randomUUID();
      const order = await orderService.submit(
        {
          account_id: account.trim(),
          symbol,
          side,
          order_type: type,
          quantity,
          entry_price: entry || undefined,
          stop_loss: stop || undefined,
          take_profit: target || undefined,
        },
        key,
      );
      // Whatever the backend says the state is. No optimistic state, ever.
      setPlaced(order);
    } catch (err) {
      setOutcome(err instanceof ApiError ? err.message : "the submission failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <form
        aria-label="order ticket"
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setConfirming(true);
        }}
      >
        <div className="grid grid-cols-2 gap-2">
          <Button
            variant="buy"
            aria-pressed={side === "buy"}
            className={side === "buy" ? "ring-1 ring-good" : ""}
            onClick={() => setSide("buy")}
          >
            BUY
          </Button>
          <Button
            variant="sell"
            aria-pressed={side === "sell"}
            className={side === "sell" ? "ring-1 ring-critical" : ""}
            onClick={() => setSide("sell")}
          >
            SELL
          </Button>
        </div>

        <Field
          label="Account"
          hint="The account whose order manager holds this order. Empty means no venue is registered, and the backend refuses."
        >
          <Input
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            aria-label="Account"
          />
        </Field>

        <Field label="Order type">
          <Select value={type} onChange={(e) => setType(e.target.value as typeof type)}>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
        </Field>

        <div className="grid grid-cols-2 gap-2">
          <Field label="Quantity">
            <Input
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              inputMode="decimal"
            />
          </Field>
          <Field label="Entry">
            <Input
              value={entry}
              onChange={(e) => setEntry(e.target.value)}
              placeholder={type === "market" ? "market" : "price"}
              disabled={type === "market"}
              inputMode="decimal"
            />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <Field label="Stop loss">
            <Input value={stop} onChange={(e) => setStop(e.target.value)} inputMode="decimal" />
          </Field>
          <Field label="Take profit">
            <Input value={target} onChange={(e) => setTarget(e.target.value)} inputMode="decimal" />
          </Field>
        </div>

        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 rounded border border-line bg-surface-2/40 p-2 text-body">
          <dt className="text-muted">Risk / reward</dt>
          <dd className="tabular text-right text-ink-2">
            {risk?.ratio ? `1 : ${risk.ratio.toFixed(2)}` : "—"}
          </dd>
          <dt className="text-muted">Estimated risk</dt>
          <dd className="tabular text-right text-ink-2">
            {risk?.estimated ? risk.estimated.toFixed(2) : "—"}
          </dd>
          <dt className="text-muted">Mode</dt>
          <dd className="text-right text-ink-2">{health?.trading_mode?.toUpperCase() ?? "UNKNOWN"}</dd>
        </dl>

        <Button
          variant="primary"
          type="submit"
          disabled={busy || !account.trim()}
          aria-label="submit order"
        >
          {busy ? "Submitting…" : `Submit ${side.toUpperCase()}`}
        </Button>
      </form>

      {outcome && (
        <p role="alert" className="text-body text-warning">
          {outcome}
        </p>
      )}

      {placed && (
        <dl
          aria-label="order result"
          className="grid grid-cols-2 gap-x-3 gap-y-1 rounded border border-line bg-surface-2/40 p-2 text-body"
        >
          <dt className="text-muted">Status</dt>
          <dd className="text-right font-mono text-ink">{placed.status}</dd>
          <dt className="text-muted">Requested</dt>
          <dd className="text-right font-mono text-ink-2">{placed.quantity}</dd>
          {/* Filled is shown beside requested, never instead of it. */}
          <dt className="text-muted">Filled</dt>
          <dd className="text-right font-mono text-ink">{placed.filled_quantity}</dd>
          <dt className="text-muted">Avg fill</dt>
          <dd className="text-right font-mono text-ink-2">
            {placed.average_fill_price ?? "—"}
          </dd>
          <dt className="text-muted">Broker id</dt>
          <dd className="text-right font-mono text-ink-2">{placed.broker_order_id ?? "—"}</dd>
          {placed.reject_reason && (
            <>
              <dt className="text-muted">Reason</dt>
              <dd className="text-right text-critical">{placed.reject_reason}</dd>
            </>
          )}
        </dl>
      )}

      <ConfirmDialog
        open={confirming}
        title="Submit this order?"
        body={
          <>
            <p className="font-mono">
              {side.toUpperCase()} {quantity} {symbol} ({type})
            </p>
            <p className="mt-2 text-muted">
              The backend decides the traded quantity: what you typed is an input to
              position sizing, and the Risk Engine can still refuse. Mode is{" "}
              {health?.trading_mode?.toUpperCase() ?? "UNKNOWN"}.
            </p>
          </>
        }
        confirmLabel="Submit"
        onConfirm={onConfirm}
        onCancel={() => setConfirming(false)}
      />
    </div>
  );
}
