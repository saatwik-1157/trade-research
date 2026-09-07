"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import { botService, type BotRow, type BotState } from "@/lib/services";

/**
 * The bot table. One component, rendered both in the terminal and on the bots
 * page — a second table over the same rows would be a second place for the
 * health rule to drift.
 *
 * **Health is shown as measured, not as recorded.** A run whose row says
 * `running` and whose heartbeat has stopped renders as stale, because "the
 * database says RUNNING" is not evidence that anything is running, and an
 * operator reading a green row would have no reason to look further.
 */
const TONE: Record<BotState, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  starting: "accent",
  running: "good",
  // Deliberately not neutral: a paused bot is configured and deliberately not
  // trading, which is a state somebody chose and should be visible as such.
  paused: "warning",
  stopping: "warning",
  stopped: "neutral",
  crashed: "critical",
  // Somebody is acting on it right now — different from crashed, where nobody
  // is, and only this one means an answer is coming.
  recovering: "warning",
  halted: "critical",
  disabled: "neutral",
};

export function BotStatus() {
  const query = useQuery({ queryKey: ["bots"], queryFn: botService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: BotRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<BotRow>[] = [
    { key: "name", header: "Bot", render: (r) => r.name },
    { key: "mode", header: "Mode", render: (r) => r.mode.toUpperCase() },
    {
      key: "status",
      header: "Status",
      render: (r) =>
        r.status ? (
          <Badge tone={TONE[r.status] ?? "neutral"}>{r.status}</Badge>
        ) : (
          <span className="text-muted">never run</span>
        ),
    },
    { key: "health", header: "Heartbeat", render: (r) => <Heartbeat row={r} /> },
    { key: "limits", header: "Limits", render: (r) => <Limits limits={r.limits} /> },
    {
      key: "disabled",
      header: "Disabled",
      render: (r) =>
        r.disabled ? (
          <span className="text-critical" title={r.disabled_reason ?? undefined}>
            yes
          </span>
        ) : (
          <span className="text-muted">no</span>
        ),
    },
    { key: "started", header: "Started", align: "right", render: (r) => r.started_at ?? "—" },
    { key: "reason", header: "Stop reason", render: (r) => r.stop_reason ?? "—" },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.bot_id}
        loading={query.isPending}
        empty="no bots registered"
        emptyHint="Bots run in backend workers, not in this browser."
        caption="Bots"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
    </div>
  );
}

/**
 * The heartbeat, and whether it has stopped.
 *
 * A stale one is the single most important thing this table can say, because
 * every other column would still read as healthy.
 */
function Heartbeat({ row }: { row: BotRow }) {
  if (row.heartbeat_age_seconds === null) {
    return <span className="text-muted">—</span>;
  }
  const age = `${Math.round(row.heartbeat_age_seconds)}s ago`;
  if (row.heartbeat_stale) {
    return (
      <span
        className="font-mono text-critical"
        title="the process is gone; the status column is stale"
      >
        {age} ✗
      </span>
    );
  }
  return <span className="font-mono text-ink-2">{age}</span>;
}

/**
 * Only the limits that are actually set. A dash means this bot states nothing
 * and the account's limits apply — NOT that it is unlimited.
 */
function Limits({ limits }: { limits: BotRow["limits"] }) {
  const parts: string[] = [];
  if (limits.max_positions !== null) parts.push(`${limits.max_positions} pos`);
  if (limits.max_daily_trades !== null) parts.push(`${limits.max_daily_trades}/day`);
  if (limits.max_daily_loss !== null) parts.push(`-${limits.max_daily_loss}`);
  if (limits.cooldown_seconds) parts.push(`${limits.cooldown_seconds}s cool`);
  if (parts.length === 0) {
    return (
      <span className="text-muted" title="this bot states no limits; the account's apply">
        —
      </span>
    );
  }
  return <span className="font-mono text-micro text-ink-2">{parts.join(" · ")}</span>;
}
