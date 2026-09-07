"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  Select,
  StatTile,
  type Column,
} from "@/components/ui";
import { TradeReview } from "@/components/TradeReview";
import {
  journalService,
  type TradeFilters,
  type TradeRow,
  type TradeStatus,
} from "@/lib/services";

/**
 * The trade journal. Sections 36 to 39.
 *
 * **Real rows only.** §51. When nothing has been journalled the table says NO
 * TRADES rather than showing a sample — a demonstration row is indistinguishable
 * from a recorded one at a glance, and this repository's whole discipline is
 * that a generated figure must never look like a measured one.
 *
 * **R is the figure to read, not net currency.** `CLAUDE.md` records why: a
 * trade's size is set by its stop distance, and pooling net currency across
 * trades sized differently is structurally the metals-points error wearing a lot
 * size. Both columns are shown and the header says which pools.
 *
 * **Paper and live are never merged in a figure.** §30. Every row carries its
 * `mode` and the badge is the first thing in it; the filter is opt-in rather
 * than defaulted, because defaulting to paper would hide live trades from
 * somebody who asked for all of them.
 *
 * **System facts and user notes are kept apart.** §39. Everything on this page
 * is system-generated and read-only. Notes are `journal_entries`, a separate
 * table, and no control here can overwrite a recorded fact.
 */
const STATUS_TONE: Record<TradeStatus, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  closed: "accent",
  open: "neutral",
  partially_closed: "warning",
  // The venue disagrees. Not a finished trade, and not a fault in the figures.
  reconciliation_required: "critical",
  unknown: "warning",
};

const MODE_TONE: Record<string, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  paper: "neutral",
  demo: "neutral",
  live: "warning",
};

function money(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const asNumber = Number(value);
  if (!Number.isFinite(asNumber)) return value;
  return asNumber.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function ratio(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const asNumber = Number(value);
  return Number.isFinite(asNumber) ? asNumber.toFixed(2) : value;
}

function when(value: string): string {
  return value.replace("T", " ").slice(0, 16);
}

export function TradeJournal() {
  const [filters, setFilters] = useState<TradeFilters>({ limit: 25 });
  const [selected, setSelected] = useState<string | null>(null);

  const trades = useQuery({
    queryKey: ["trades", filters],
    queryFn: () => journalService.list(filters),
  });
  const stats = useQuery({
    queryKey: ["trade-statistics", filters.mode],
    queryFn: () => journalService.statistics({ mode: filters.mode }),
  });

  const columns: Column<TradeRow>[] = [
    {
      key: "mode",
      header: "Env",
      render: (r) => <Badge tone={MODE_TONE[r.mode] ?? "neutral"}>{r.mode}</Badge>,
    },
    { key: "closed_at", header: "Closed", render: (r) => when(r.closed_at) },
    { key: "symbol", header: "Symbol", render: (r) => r.symbol ?? "—" },
    {
      key: "side",
      header: "Side",
      render: (r) => <Badge tone={r.side === "long" ? "accent" : "warning"}>{r.side}</Badge>,
    },
    { key: "volume", header: "Qty", align: "right", render: (r) => r.volume },
    { key: "entry_price", header: "Entry", align: "right", render: (r) => r.entry_price },
    { key: "exit_price", header: "Exit", align: "right", render: (r) => r.exit_price },
    {
      key: "r_multiple",
      header: "R",
      align: "right",
      render: (r) => <span className="font-semibold">{ratio(r.r_multiple)}</span>,
    },
    { key: "net_profit", header: "Net", align: "right", render: (r) => money(r.net_profit) },
    { key: "exit_reason", header: "Exit reason", render: (r) => r.exit_reason ?? "—" },
    {
      key: "status",
      header: "Status",
      render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "neutral"}>{r.status}</Badge>,
    },
    {
      key: "detail",
      header: "",
      render: (r) => (
        <button
          type="button"
          className="text-body underline"
          onClick={() => setSelected(r.id)}
          aria-label={`Open trade ${r.id}`}
        >
          detail
        </button>
      ),
    },
  ];

  const rows = trades.data?.items ?? [];

  return (
    <div className="space-y-4">
      <Panel title="Journal" level={31}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Select
            aria-label="Environment"
            value={filters.mode ?? ""}
            onChange={(e) => setFilters({ ...filters, mode: e.target.value || undefined })}
          >
            <option value="">All environments</option>
            <option value="paper">paper</option>
            <option value="demo">demo</option>
            <option value="live">live</option>
          </Select>
          <Select
            aria-label="Result"
            value={filters.result ?? ""}
            onChange={(e) => setFilters({ ...filters, result: e.target.value || undefined })}
          >
            <option value="">Any result</option>
            <option value="win">wins</option>
            <option value="loss">losses</option>
            <option value="flat">flat</option>
          </Select>
          <a
            className="text-body underline"
            href={journalService.exportUrl(filters)}
            download="trades.csv"
          >
            export CSV
          </a>
        </div>

        {stats.data ? (
          <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-5">
            <StatTile label="Trades" value={String(stats.data.trades)} />
            <StatTile
              label="Win rate"
              value={
                stats.data.win_rate === null
                  ? undefined
                  : `${(stats.data.win_rate * 100).toFixed(1)}%`
              }
              note={`${stats.data.wins}W / ${stats.data.losses}L`}
            />
            <StatTile
              label="Total R"
              value={ratio(stats.data.r_multiple.sum)}
              note={`${stats.data.r_multiple.trades_with_r} trades carry R`}
            />
            <StatTile label="Mean R" value={ratio(stats.data.r_multiple.mean)} />
            <StatTile label="Net" value={money(stats.data.net_profit)} note="not poolable" />
          </div>
        ) : null}

        <p className="mb-3 text-body text-muted">
          Read <strong>R</strong>, not net currency: a trade&apos;s size is set by its stop
          distance, and pooling net across trades sized differently adds numbers that are not the
          same quantity. Sharpe, Sortino and significance are L32&apos;s, deliberately absent here.
        </p>

        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.id}
          loading={trades.isLoading}
          error={trades.isError ? "The journal could not be read." : null}
          empty="NO TRADES"
          emptyHint="Nothing has been journalled. A trade is recorded when a position closes and the venue confirms it -- no sample rows are shown."
        />
        {trades.data ? (
          <p className="mt-2 text-body text-muted">
            {rows.length} of {trades.data.page.total}
          </p>
        ) : null}
      </Panel>

      {selected ? <TradeDetail tradeId={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  );
}

export function TradeDetail({ tradeId, onClose }: { tradeId: string; onClose?: () => void }) {
  const trade = useQuery({ queryKey: ["trade", tradeId], queryFn: () => journalService.get(tradeId) });
  const decisions = useQuery({
    queryKey: ["trade-decisions", tradeId],
    queryFn: () => journalService.decisions(tradeId),
  });
  const timeline = useQuery({
    queryKey: ["trade-timeline", tradeId],
    queryFn: () => journalService.timeline(tradeId),
  });
  const executions = useQuery({
    queryKey: ["trade-executions", tradeId],
    queryFn: () => journalService.executions(tradeId),
  });

  if (trade.isLoading) return <LoadingState what="the trade" />;
  if (trade.isError) return <ErrorState message="That trade could not be read." />;
  const row = trade.data;
  if (!row) return null;

  return (
    <Panel
      title={`Trade ${row.id}`}
      level={31}
      right={
        onClose ? (
          <button type="button" className="text-body underline" onClick={onClose}>
            close
          </button>
        ) : undefined
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Badge tone={MODE_TONE[row.mode] ?? "neutral"}>{row.mode}</Badge>
        <Badge tone={STATUS_TONE[row.status] ?? "neutral"}>{row.status}</Badge>
        {row.data_quality && row.data_quality.errors > 0 ? (
          <Badge tone="critical">{`${row.data_quality.errors} data-quality error(s)`}</Badge>
        ) : null}
      </div>

      {row.data_quality?.findings.length ? (
        <ul className="mb-3 space-y-1 text-body text-warning">
          {row.data_quality.findings.map((finding) => (
            <li key={finding.code}>
              <strong>{finding.code}</strong>: {finding.detail}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Entry" value={row.entry_price} />
        <StatTile label="Exit" value={row.exit_price} />
        <StatTile label="Quantity" value={row.volume} />
        <StatTile label="R" value={ratio(row.r_multiple)} />
        <StatTile label="Gross" value={money(row.gross_profit)} />
        <StatTile label="Commission" value={money(row.commission)} />
        <StatTile label="Swap" value={money(row.swap)} />
        <StatTile
          label="Net"
          value={money(row.net_profit)}
          note={row.currency ?? "currency not recorded"}
        />
        <StatTile
          label="Held"
          value={
            decisions.data?.holding.hours === null || decisions.data?.holding.hours === undefined
              ? undefined
              : `${decisions.data.holding.hours.toFixed(2)} h`
          }
        />
        <StatTile label="Exit reason" value={row.exit_reason ?? undefined} />
      </div>

      <section className="mt-4">
        <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">Context</h4>
        <div className="grid gap-3 md:grid-cols-2">
          {(["strategy", "ai", "risk", "sizing"] as const).map((key) => {
            const block = decisions.data?.[key] as
              | { available?: boolean; why?: string }
              | undefined;
            return (
              <div key={key} className="rounded border border-line p-2">
                <div className="mb-1 text-mini font-semibold uppercase text-muted">{key}</div>
                {block?.available === false ? (
                  <p className="text-body text-muted">{block.why}</p>
                ) : (
                  <pre className="overflow-x-auto text-micro leading-tight">
                    {JSON.stringify(block ?? {}, null, 1)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section className="mt-4">
        <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">Closes</h4>
        {executions.data?.available ? (
          <>
            <table className="w-full text-body">
              <thead className="text-muted">
                <tr>
                  <th className="text-left font-normal">At</th>
                  <th className="text-right font-normal">Qty</th>
                  <th className="text-right font-normal">Fill</th>
                  <th className="text-left font-normal">Reason</th>
                </tr>
              </thead>
              <tbody>
                {(executions.data.closes ?? []).map((close) => (
                  <tr key={`${close.at}-${close.quantity}`}>
                    <td className="py-0.5">{when(close.at)}</td>
                    <td className="py-0.5 text-right">{close.quantity}</td>
                    <td className="py-0.5 text-right">{close.fill_price}</td>
                    <td className="py-0.5">{close.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-1 text-body text-muted">{executions.data.exit?.note}</p>
          </>
        ) : (
          <p className="text-body text-muted">{executions.data?.why ?? "—"}</p>
        )}
      </section>

      <section className="mt-4">
        {/* L33. The review reads the same trade and changes nothing about it. */}
        <TradeReview tradeId={tradeId} />
      </section>

      <section className="mt-4">
        <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">Timeline</h4>
        {timeline.data?.events.length ? (
          <ol className="space-y-1">
            {timeline.data.events.map((event, index) => (
              <li key={`${event.at}-${event.kind}-${index}`} className="text-body">
                <span className="font-mono text-muted">{when(event.at)}</span>{" "}
                <strong>{event.kind}</strong>{" "}
                <span className="text-muted">({event.source_note})</span>
              </li>
            ))}
          </ol>
        ) : (
          <EmptyState message="No recorded events" />
        )}
        {timeline.data?.gaps.length ? (
          <ul className="mt-2 space-y-1 text-body text-muted">
            {timeline.data.gaps.map((gap) => (
              <li key={gap}>· {gap}</li>
            ))}
          </ul>
        ) : null}
      </section>
    </Panel>
  );
}
