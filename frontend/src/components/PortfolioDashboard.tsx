"use client";

import { useMemo, useState } from "react";
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
import {
  accountsService,
  portfolioService,
  type AccountOption,
  type BucketRow,
  type PortfolioHealthState,
  type PortfolioPositionRow,
} from "@/lib/services";

/**
 * The portfolio dashboard. Sections 36 to 39 of the level brief.
 *
 * **A dash is not a zero.** Every monetary field arrives as a string or `null`,
 * and a `null` renders as `—`. A balance column showing `0.00` for a broker
 * that could not be read is the failure §59 names, and it is the one a reader
 * would act on without noticing.
 *
 * **Gross and net are shown side by side, always.** §11 and §12. A single
 * "exposure" tile would be the number that understates a hedged book by more
 * than half, and which of the two it meant would depend on who wrote it.
 *
 * **STALE is not a shade of healthy.** §31 and §45. When the view is stale or
 * needs reconciliation the reasons are shown above the figures, because the
 * numbers are still real — they are just no longer current, and that is a
 * different thing to know than "the account is down".
 *
 * **Paper and live are chosen, never merged.** §41. One account at a time, and
 * its environment is on the badge beside every figure.
 */
const HEALTH_TONE: Record<PortfolioHealthState, "good" | "warning" | "critical" | "neutral" | "accent"> =
  {
    HEALTHY: "accent",
    WARNING: "warning",
    // Real figures, and old. Not a fault, and not fine either.
    STALE: "warning",
    RECONCILIATION_REQUIRED: "critical",
    ERROR: "critical",
  };

const ENV_TONE: Record<string, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  paper: "neutral",
  demo: "neutral",
  live: "warning",
};

/**
 * A money string, or `undefined` so the tile renders its own "no data".
 *
 * `undefined` rather than a literal dash: `StatTile` already knows how to show
 * an absent figure and labels it for a screen reader, and a second em dash
 * here would look identical while being invisible to that label.
 */
function money(value: string | null | undefined, currency?: string | null): string | undefined {
  if (value === null || value === undefined) return undefined;
  const asNumber = Number(value);
  if (!Number.isFinite(asNumber)) return value;
  const formatted = asNumber.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return currency ? `${formatted} ${currency}` : formatted;
}

function percent(value: number | null | undefined): string | undefined {
  if (value === null || value === undefined) return undefined;
  return `${(value * 100).toFixed(2)}%`;
}

/** The same figure inside a table cell, where there is no tile to fall back on. */
function cell(value: string | null | undefined): string {
  return money(value) ?? "—";
}

export function PortfolioDashboard() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: accountsService.list });
  const [selected, setSelected] = useState<string>("");
  const options: AccountOption[] = accounts.data ?? [];
  const accountId = selected || options[0]?.id || "";
  const account = options.find((a) => a.id === accountId);

  if (accounts.isLoading) return <LoadingState what="accounts" />;
  if (accounts.isError) return <ErrorState message="Accounts could not be loaded." />;
  if (options.length === 0) {
    return (
      <EmptyState
        message="No trading accounts"
        hint="The portfolio reads an account that exists. Create a paper account, or connect a broker account, and it will appear here."
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Select
          value={accountId}
          onChange={(event) => setSelected(event.target.value)}
          aria-label="Account"
        >
          {options.map((option) => (
            <option key={option.id} value={option.id}>
              {`${option.name} (${option.environment})`}
            </option>
          ))}
        </Select>
        {account ? <Badge tone={ENV_TONE[account.environment] ?? "neutral"}>{account.environment}</Badge> : null}
      </div>
      <Summary accountId={accountId} />
      <Exposure accountId={accountId} />
      <Positions accountId={accountId} />
      <Reconciliation accountId={accountId} />
    </div>
  );
}

function Summary({ accountId }: { accountId: string }) {
  const query = useQuery({
    queryKey: ["portfolio-summary", accountId],
    queryFn: () => portfolioService.summary(accountId),
    enabled: Boolean(accountId),
  });

  if (query.isLoading) return <LoadingState what="the account" />;
  if (query.isError) return <ErrorState message="The portfolio could not be read." />;
  const view = query.data;
  if (!view) return null;
  const currency = view.account.currency;

  return (
    <Panel title="Account" level={30}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Badge tone={HEALTH_TONE[view.health] ?? "neutral"}>{view.health}</Badge>
        <Badge tone={view.freshness === "FRESH" ? "accent" : "warning"}>{view.freshness}</Badge>
        <span className="text-body text-muted">source: {view.account.source}</span>
      </div>
      {view.health !== "HEALTHY" ? (
        <ul className="mb-3 space-y-1 text-body text-warning">
          {view.health_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Balance" value={money(view.account.balance, currency)} />
        <StatTile label="Equity" value={money(view.account.equity, currency)} />
        <StatTile label="Margin used" value={money(view.account.margin_used, currency)} />
        <StatTile label="Margin free" value={money(view.account.margin_free, currency)} />
        <StatTile label="Realized" value={money(view.pnl?.realized, currency)} />
        <StatTile
          label="Unrealized"
          value={money(view.pnl?.unrealized, currency)}
          note={
            view.pnl?.unrealized_unavailable.length
              ? `no mark for ${view.pnl.unrealized_unavailable.join(", ")}`
              : undefined
          }
        />
        <StatTile label="Realized today" value={money(view.pnl?.realized_today, currency)} />
        <StatTile
          label="Drawdown"
          value={percent(view.drawdown?.current_pct)}
          note={`peak ${cell(view.drawdown?.peak_equity)}`}
        />
      </div>
      {view.pnl?.unrealized === null ? (
        <p className="mt-3 text-body text-muted">
          Unrealized P&amp;L is withheld rather than partial: a total missing one position reads as
          a complete figure. The positions it could not mark are named above.
        </p>
      ) : null}
    </Panel>
  );
}

function Exposure({ accountId }: { accountId: string }) {
  const query = useQuery({
    queryKey: ["portfolio-exposure", accountId],
    queryFn: () => portfolioService.exposure(accountId),
    enabled: Boolean(accountId),
  });

  const report = query.data?.exposure ?? null;
  const groups = useMemo(() => {
    if (!report) return [];
    return [
      { title: "By symbol", rows: report.by_symbol },
      { title: "By strategy", rows: report.by_strategy },
      { title: "By bot", rows: report.by_bot },
      { title: "By asset class", rows: report.by_asset_class },
    ];
  }, [report]);

  if (query.isLoading) return <LoadingState what="exposure" />;
  if (query.isError) return <ErrorState message="Exposure could not be computed." />;
  if (!report) return null;

  return (
    <Panel title="Exposure" level={30}>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Gross" value={money(report.total.gross)} />
        <StatTile label="Net" value={money(report.total.net)} />
        <StatTile label="Long" value={money(report.total.long)} />
        <StatTile label="Short" value={money(report.total.short)} />
      </div>
      {report.total.uncomputable > 0 ? (
        <p className="mt-3 text-body text-warning">
          {report.total.uncomputable} position(s) have no measured contract size, so their notional
          could not be computed. They are excluded from the totals and counted here rather than
          treated as zero.
        </p>
      ) : null}

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        {groups.map((group) => (
          <div key={group.title}>
            <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">
              {group.title}
            </h4>
            {Object.keys(group.rows).length === 0 ? (
              <p className="text-body text-muted">nothing open</p>
            ) : (
              <table className="w-full text-body">
                <thead className="text-muted">
                  <tr>
                    <th className="text-left font-normal">Key</th>
                    <th className="text-right font-normal">Gross</th>
                    <th className="text-right font-normal">Net</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(group.rows).map(([key, bucket]: [string, BucketRow]) => (
                    <tr key={key}>
                      <td className="py-0.5 font-mono">{key}</td>
                      <td className="py-0.5 text-right">{cell(bucket.gross)}</td>
                      <td className="py-0.5 text-right">{cell(bucket.net)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>

      <div className="mt-4">
        <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">By currency</h4>
        {report.by_currency === null ? (
          <p className="text-body text-warning">{report.currency_note}</p>
        ) : (
          <table className="w-full max-w-sm text-body">
            <tbody>
              {Object.entries(report.by_currency).map(([code, value]) => (
                <tr key={code}>
                  <td className="py-0.5 font-mono">{code}</td>
                  <td className="py-0.5 text-right">{cell(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="mt-4 space-y-1 text-body text-muted">
        <p>
          Open risk to stop: {cell(query.data?.open_risk.total)}
          {query.data && !query.data.open_risk.complete ? ` — ${query.data.open_risk.note}` : ""}
        </p>
        <p>{query.data?.correlation.reason}</p>
      </div>
    </Panel>
  );
}

function Positions({ accountId }: { accountId: string }) {
  const query = useQuery({
    queryKey: ["portfolio-positions", accountId],
    queryFn: () => portfolioService.positions(accountId),
    enabled: Boolean(accountId),
  });

  const columns: Column<PortfolioPositionRow>[] = [
    { key: "symbol", header: "Symbol", render: (r) => <span className="font-mono">{r.symbol}</span> },
    {
      key: "side",
      header: "Side",
      render: (r) => <Badge tone={r.side === "long" ? "accent" : "warning"}>{r.side}</Badge>,
    },
    { key: "quantity", header: "Qty", render: (r) => r.quantity },
    { key: "entry_price", header: "Entry", render: (r) => r.entry_price },
    { key: "current_price", header: "Mark", render: (r) => r.current_price ?? "—" },
    { key: "unrealized_pnl", header: "Unrealized", render: (r) => cell(r.unrealized_pnl) },
    { key: "stop_loss", header: "Stop", render: (r) => r.stop_loss ?? "—" },
    {
      key: "notional",
      header: "Notional",
      render: (r) =>
        r.notional.computable ? (
          cell(r.notional.value)
        ) : (
          <span title={r.notional.reason} className="text-warning">
            uncomputable
          </span>
        ),
    },
    { key: "strategy_id", header: "Strategy", render: (r) => r.strategy_id ?? "—" },
    { key: "bot_id", header: "Bot", render: (r) => r.bot_id ?? "—" },
  ];

  if (query.isLoading) return <LoadingState what="positions" />;
  if (query.isError) return <ErrorState message="Positions could not be read." />;
  const rows = query.data?.positions ?? [];

  return (
    <Panel title="Open positions" level={30}>
      {rows.length === 0 ? (
        <EmptyState
          message="No open positions"
          hint="The platform holds nothing on this account. That is a fact about the platform's own record, not a claim about the broker's book."
        />
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.position_id} />
      )}
      {query.data?.unmarked.length ? (
        <p className="mt-3 text-body text-muted">
          No live quote for {query.data.unmarked.join(", ")}. Those positions are shown unmarked
          rather than valued at their entry price, which would be a stale figure presented as a
          current one.
        </p>
      ) : null}
    </Panel>
  );
}

function Reconciliation({ accountId }: { accountId: string }) {
  const query = useQuery({
    queryKey: ["portfolio-reconciliation", accountId],
    queryFn: () => portfolioService.reconciliation(accountId),
    enabled: Boolean(accountId),
  });

  const found = query.data?.reconciliation;
  if (!found) return null;

  return (
    <Panel title="Reconciliation" level={30}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={!found.checked ? "neutral" : found.agrees ? "accent" : "critical"}>
          {!found.checked ? "NOT CHECKED" : found.agrees ? "AGREES" : "MISMATCH"}
        </Badge>
        <span className="text-body text-muted">
          internal {found.internal_positions} · broker {found.broker_positions}
        </span>
      </div>
      {found.mismatches.length > 0 ? (
        <ul className="mt-2 space-y-1 text-body text-critical">
          {found.mismatches.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
      <p className="mt-2 text-body text-muted">{found.note}</p>
    </Panel>
  );
}
