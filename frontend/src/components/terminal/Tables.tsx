"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import {
  orderService,
  positionService,
  type OrderRow,
  type OrderState,
  type PositionRow,
  type PositionState,
} from "@/lib/services";

const ORDER_TONE: Record<OrderState, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  intent: "neutral",
  // In flight to the venue. Amber rather than neutral: a submitting order
  // whose process died is the case reconciliation exists for.
  submitting: "warning",
  submitted: "accent",
  accepted: "accent",
  partially_filled: "warning",
  filled: "good",
  cancel_requested: "warning",
  cancelled: "neutral",
  rejected: "critical",
  expired: "neutral",
  failed: "critical",
  // Not an error to tidy away. We do not know what the venue did.
  unknown: "warning",
};

const POSITION_TONE: Record<
  PositionState,
  "good" | "warning" | "critical" | "neutral" | "accent"
> = {
  // No fill has confirmed this exists yet.
  opening: "warning",
  open: "accent",
  partially_closed: "accent",
  // A close was asked for and the venue has not confirmed it.
  closing: "warning",
  closed: "neutral",
  // Not an error to tidy away. We do not know what the venue holds.
  unknown: "critical",
  reconciling: "warning",
};

export function PositionsTable() {
  const query = useQuery({ queryKey: ["positions"], queryFn: positionService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: PositionRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<PositionRow>[] = [
    { key: "symbol", header: "Symbol", render: (r) => r.symbol ?? "—" },
    { key: "side", header: "Side", render: (r) => r.side },
    { key: "qty", header: "Open", align: "right", render: (r) => r.quantity },
    // What it opened at, beside what is left. A position cut down by a
    // scale-out is not the position that was risk-sized, and showing only the
    // remainder would hide that.
    {
      key: "initial",
      header: "Opened",
      align: "right",
      render: (r) => <span className="text-muted">{r.initial_quantity}</span>,
    },
    { key: "entry", header: "Entry", align: "right", render: (r) => r.entry_price },
    {
      key: "sl",
      header: "SL",
      align: "right",
      render: (r) => <Protection intended={r.stop_loss} venue={r.broker_stop_loss} synced={r.broker_synced_at} />,
    },
    {
      key: "tp",
      header: "TP",
      align: "right",
      render: (r) => <Protection intended={r.take_profit} venue={r.broker_take_profit} synced={r.broker_synced_at} />,
    },
    {
      key: "realized",
      header: "Realized",
      align: "right",
      render: (r) => (r.realized_pnl ? <span className="font-mono">{r.realized_pnl}</span> : "—"),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <Badge tone={POSITION_TONE[r.status] ?? "neutral"}>{r.status}</Badge>
      ),
    },
    {
      key: "broker",
      header: "Broker id",
      render: (r) => <span className="font-mono">{r.broker_position_id ?? "—"}</span>,
    },
    { key: "mode", header: "Mode", render: (r) => r.mode },
    { key: "opened", header: "Opened at", align: "right", render: (r) => r.opened_at },
  ];

  return (
    <div className="flex h-full flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="the platform has recorded no positions"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
    </div>
  );
}

/**
 * A protective level, and whether the venue is holding it.
 *
 * Three states, and they are deliberately not collapsed. A level the venue
 * has never been asked about is UNKNOWN, not agreed; a level the venue is not
 * holding is the most dangerous thing this table can show, because the screen
 * would otherwise say protected while the position is not.
 */
function Protection({
  intended,
  venue,
  synced,
}: {
  intended: string | null;
  venue: string | null;
  synced: string | null;
}) {
  if (intended === null) return <span className="text-muted">—</span>;
  if (synced === null) {
    return (
      <span className="font-mono text-ink-2" title="never read from the venue">
        {intended} <span className="text-muted">?</span>
      </span>
    );
  }
  if (venue === null) {
    return (
      <span
        className="font-mono text-critical"
        title="the record says this level is set and the venue is not holding it"
      >
        {intended} ✗
      </span>
    );
  }
  if (venue !== intended) {
    return (
      <span className="font-mono text-warning" title={`the venue is holding ${venue}`}>
        {intended} ≠ {venue}
      </span>
    );
  }
  return <span className="font-mono text-ink-2">{intended}</span>;
}

export function OrdersTable() {
  const query = useQuery({ queryKey: ["orders"], queryFn: orderService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: OrderRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<OrderRow>[] = [
    { key: "id", header: "Order", render: (r) => <span className="font-mono">{r.id.slice(0, 8)}</span> },
    { key: "symbol", header: "Symbol", render: (r) => r.symbol ?? "—" },
    { key: "side", header: "Side", render: (r) => r.side },
    { key: "type", header: "Type", render: (r) => r.order_type },
    { key: "qty", header: "Qty", align: "right", render: (r) => r.quantity },
    // Requested and filled are separate columns on purpose: a requested
    // quantity is not an executed one until the venue says so, and a table
    // that showed only one could not show the difference.
    {
      key: "filled",
      header: "Filled",
      align: "right",
      render: (r) => <span className="font-mono">{r.filled_quantity}</span>,
    },
    {
      key: "avg",
      header: "Avg fill",
      align: "right",
      render: (r) => r.average_fill_price ?? "—",
    },
    { key: "sl", header: "SL", align: "right", render: (r) => r.stop_loss ?? "—" },
    { key: "tp", header: "TP", align: "right", render: (r) => r.take_profit ?? "—" },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <Badge tone={ORDER_TONE[r.status] ?? "neutral"} title={r.reject_reason ?? undefined}>
          {r.status}
        </Badge>
      ),
    },
    {
      key: "broker",
      header: "Broker id",
      render: (r) => <span className="font-mono">{r.broker_order_id ?? "—"}</span>,
    },
    { key: "mode", header: "Mode", render: (r) => r.mode },
    { key: "created", header: "Created", align: "right", render: (r) => r.created_at },
  ];

  return (
    <div className="flex h-full flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no orders recorded"
        emptyHint="Nothing has been submitted, because submission is not built."
        caption="Orders"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
    </div>
  );
}
