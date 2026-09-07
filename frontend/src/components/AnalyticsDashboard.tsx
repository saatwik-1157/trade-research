"use client";

import { useMemo, useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
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
  INSUFFICIENT_DATA,
  analyticsService,
  type AnalyticsFilters,
  type AnalyticsSummary,
  type Metric,
  type MetricBlock,
} from "@/lib/services";

/**
 * The analytics dashboard. Sections 32 to 36.
 *
 * **N/A is not zero, and INSUFFICIENT_DATA is not a failure.** §33. A metric
 * the backend could not compute from the sample arrives as the sentinel string
 * and renders as a dash with the reason beside it — never as 0.00, which a
 * reader acts on, and never as a blank, which reads as a bug.
 *
 * **No demo data, ever.** §34 and §48. With no trades the charts show an empty
 * state. A sample equity curve is indistinguishable from a real one at a glance,
 * and this repository's entire discipline is that a generated figure must never
 * look like a measured one.
 *
 * **R and currency are shown side by side, labelled.** Net currency cannot be
 * pooled across trades sized by different stop distances; R can. Showing only
 * one would leave the reader to guess which they were looking at.
 *
 * **Environments are chosen, never merged.** §23. The picker defaults to "all",
 * and the summary reports which environments the rows it summarised spanned —
 * so a mixed set is visible as mixed rather than presented as one account.
 *
 * **The equity chart is an inline SVG.** No charting library is loaded: the
 * curve is a polyline over points the API returned, so there is no path by
 * which a library's interpolation could invent a value between two readings.
 */
const PERIODS = [
  ["all_time", "All time"],
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["last_7d", "Last 7 days"],
  ["last_30d", "Last 30 days"],
  ["last_90d", "Last 90 days"],
  ["this_month", "This month"],
  ["previous_month", "Previous month"],
  ["this_year", "This year"],
] as const;

const DIMENSIONS = [
  ["strategy", "Strategy"],
  ["symbol", "Symbol"],
  ["bot", "Bot"],
  ["exit_reason", "Exit reason"],
  ["model", "AI model"],
  ["model_version", "Model version"],
  ["ai_mode", "AI mode"],
  ["regime", "Regime"],
  ["mode", "Environment"],
  ["side", "Side"],
] as const;

/** A metric, or `undefined` so the tile renders its own labelled "no data". */
function show(value: Metric | undefined, digits = 2): string | undefined {
  if (value === undefined || value === null || value === INSUFFICIENT_DATA) return undefined;
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pct(value: Metric | undefined): string | undefined {
  if (value === undefined || value === null || value === INSUFFICIENT_DATA) return undefined;
  return `${(Number(value) * 100).toFixed(1)}%`;
}

/** The same figure in a table cell, where there is no tile to fall back on. */
function cell(value: Metric | undefined, digits = 2): string {
  return show(value, digits) ?? "N/A";
}

export function AnalyticsDashboard() {
  const [filters, setFilters] = useState<AnalyticsFilters>({ period: "all_time" });
  const [dimension, setDimension] = useState<string>("strategy");

  const summary = useQuery({
    queryKey: ["analytics-summary", filters],
    queryFn: () => analyticsService.summary(filters),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          aria-label="Environment"
          value={filters.environment ?? ""}
          onChange={(e) => setFilters({ ...filters, environment: e.target.value || undefined })}
        >
          <option value="">All environments</option>
          <option value="paper">paper</option>
          <option value="demo">demo</option>
          <option value="live">live</option>
        </Select>
        <Select
          aria-label="Period"
          value={filters.period ?? "all_time"}
          onChange={(e) => setFilters({ ...filters, period: e.target.value })}
        >
          {PERIODS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
      </div>

      <Summary query={summary} />
      <EquityCurve filters={filters} />
      <Drawdown filters={filters} />
      <Breakdown filters={filters} dimension={dimension} onDimension={setDimension} />
      <ExecutionQuality filters={filters} />
    </div>
  );
}

function Summary({
  query,
}: {
  query: UseQueryResult<AnalyticsSummary>;
}) {
  if (query.isLoading) return <LoadingState what="analytics" />;
  if (query.isError) return <ErrorState message="Analytics could not be read." />;
  const data = query.data;
  if (!data) return null;
  const money: MetricBlock = data.currency;
  const r: MetricBlock = data.r_multiple;

  if (data.trade_count === 0) {
    return (
      <Panel title="Performance" level={32}>
        <EmptyState
          message="NO COMPLETED TRADES"
          hint="Nothing matches this filter. No sample figures are shown: a demonstration equity curve is indistinguishable from a real one at a glance."
        />
      </Panel>
    );
  }

  return (
    <Panel title="Performance" level={32}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {Object.entries(data.environments as Record<string, number>).map(([name, count]) => (
          <Badge key={name} tone={name === "live" ? "warning" : "neutral"}>
            {`${name} · ${count}`}
          </Badge>
        ))}
      </div>

      {data.sample_warning ? (
        <p className="mb-3 text-body text-warning">{data.sample_warning}</p>
      ) : null}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatTile label="Trades" value={String(data.trade_count)} />
        <StatTile
          label="Win rate"
          value={pct(money.win_rate)}
          note={`${money.winning_trades}W / ${money.losing_trades}L / ${money.breakeven_trades}BE`}
        />
        <StatTile label="Net P&L" value={show(money.net_profit)} note="not poolable" />
        <StatTile label="Profit factor" value={show(money.profit_factor)} />
        <StatTile label="Expectancy" value={show(money.expectancy)} />
        <StatTile label="Total R" value={show(r.net_profit)} note="the poolable figure" />
        <StatTile label="Mean R" value={show(r.expectancy)} />
        <StatTile
          label="Sharpe / trade"
          value={show(money.sharpe_per_trade)}
          note="not annualised"
        />
        <StatTile label="Sortino / trade" value={show(money.sortino_per_trade)} />
        <StatTile
          label="t-statistic"
          value={show(money.t_statistic)}
          note={`on ${data.trade_count} trades`}
        />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatTile label="Gross profit" value={show(money.gross_profit)} />
        <StatTile label="Gross loss" value={show(money.gross_loss)} />
        <StatTile label="Commission" value={show(data.costs.commission)} />
        <StatTile label="Swap" value={show(data.costs.swap)} />
        <StatTile label="Fees" value={show(data.costs.fees)} />
      </div>

      {!data.costs.reconciles ? (
        <p className="mt-2 text-body text-critical">
          Gross less costs does not equal net for this set. A cost is booked twice or not at
          all — see the journal&apos;s data-quality findings.
        </p>
      ) : null}

      <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Longest win streak" value={String(money.streaks.longest_win)} />
        <StatTile label="Longest loss streak" value={String(money.streaks.longest_loss)} />
        <StatTile label="Largest win" value={show(money.largest_win)} />
        <StatTile label="Largest loss" value={show(money.largest_loss)} />
      </div>

      <p className="mt-3 text-body text-muted">{data.which_to_read}</p>
      <p className="mt-1 text-body text-muted">
        A dash means the figure could not be computed from this sample — not that it is zero.{" "}
        {money.sample_note}
      </p>
    </Panel>
  );
}

function EquityCurve({ filters }: { filters: AnalyticsFilters }) {
  const query = useQuery({
    queryKey: ["analytics-equity", filters],
    queryFn: () => analyticsService.equity(filters),
  });

  const path = useMemo(() => {
    const points = query.data?.realized.points ?? [];
    if (points.length < 2) return null;
    const values = points.map((p) => p.value);
    const low = Math.min(...values, 0);
    const high = Math.max(...values, 0);
    const span = high - low || 1;
    return points
      .map((p, index) => {
        const x = (index / (points.length - 1)) * 100;
        const y = 100 - ((p.value - low) / span) * 100;
        return `${x.toFixed(3)},${y.toFixed(3)}`;
      })
      .join(" ");
  }, [query.data]);

  if (query.isLoading) return <LoadingState what="the equity curve" />;
  if (query.isError) return <ErrorState message="The equity curve could not be read." />;
  const realized = query.data?.realized;

  return (
    <Panel title="Equity" level={32}>
      {!realized || realized.count === 0 ? (
        <EmptyState
          message="NO EQUITY CURVE"
          hint="No completed trades in this window. Nothing is drawn: a fabricated curve with a nicer shape is still fabricated."
        />
      ) : (
        <>
          <svg
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            className="h-40 w-full"
            role="img"
            aria-label="Cumulative realised P&L"
          >
            {path ? (
              <polyline
                points={path}
                fill="none"
                stroke="currentColor"
                strokeWidth="0.6"
                vectorEffect="non-scaling-stroke"
              />
            ) : null}
          </svg>
          <div className="mt-2 grid grid-cols-2 gap-3 md:grid-cols-3">
            <StatTile label="Points" value={String(realized.count)} />
            <StatTile label="Final" value={show(realized.final_value)} />
            <StatTile label="Environment" value={realized.environment} />
          </div>
          <p className="mt-2 text-body text-muted">{realized.note}</p>
          {query.data?.account?.available === false ? (
            <p className="mt-1 text-body text-muted">Account curve: {query.data.account.why}</p>
          ) : (
            <p className="mt-1 text-body text-warning">{query.data?.account?.note}</p>
          )}
        </>
      )}
    </Panel>
  );
}

function Drawdown({ filters }: { filters: AnalyticsFilters }) {
  const query = useQuery({
    queryKey: ["analytics-drawdown", filters],
    queryFn: () => analyticsService.drawdown(filters),
  });
  const data = query.data;

  if (query.isLoading) return <LoadingState what="drawdown" />;
  if (!data || data.available === false) {
    return (
      <Panel title="Drawdown" level={32}>
        <EmptyState message="NO DRAWDOWN" hint={data?.why ?? "No curve to measure."} />
      </Panel>
    );
  }

  return (
    <Panel title="Drawdown" level={32}>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatTile label="Peak" value={show(data.peak_equity)} />
        <StatTile label="Current" value={show(data.current_drawdown)} />
        <StatTile label="Maximum" value={show(data.max_drawdown)} />
        <StatTile label="Max %" value={pct(data.max_drawdown_pct)} />
        <StatTile label="Recovery factor" value={show(data.recovery_factor)} />
      </div>
      {data.periods?.length ? (
        <table className="mt-3 w-full text-body">
          <thead className="text-muted">
            <tr>
              <th className="text-left font-normal">Peak</th>
              <th className="text-left font-normal">Trough</th>
              <th className="text-right font-normal">Depth</th>
              <th className="text-left font-normal">Recovered</th>
            </tr>
          </thead>
          <tbody>
            {data.periods.map((period) => (
              <tr key={`${period.peak_at}-${period.trough_at}`}>
                <td className="py-0.5">{period.peak_at.slice(0, 16).replace("T", " ")}</td>
                <td className="py-0.5">{period.trough_at.slice(0, 16).replace("T", " ")}</td>
                <td className="py-0.5 text-right">{cell(period.depth)}</td>
                <td className="py-0.5">
                  {period.recovered ? "yes" : <span className="text-warning">still open</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <p className="mt-2 text-body text-muted">{data.note}</p>
    </Panel>
  );
}

type Row = {
  key: string;
  trades: number;
  win_rate: Metric;
  net_profit: number;
  profit_factor: Metric;
  expectancy: Metric;
  r_total: number;
  small: boolean;
};

function Breakdown({
  filters,
  dimension,
  onDimension,
}: {
  filters: AnalyticsFilters;
  dimension: string;
  onDimension: (value: string) => void;
}) {
  const query = useQuery({
    queryKey: ["analytics-breakdown", dimension, filters],
    queryFn: () => analyticsService.breakdown(dimension, filters),
  });

  const rows: Row[] = Object.entries(query.data?.groups ?? {}).map(([key, group]) => ({
    key,
    trades: group.trades,
    win_rate: group.currency.win_rate,
    net_profit: group.currency.net_profit,
    profit_factor: group.currency.profit_factor,
    expectancy: group.currency.expectancy,
    r_total: group.r_multiple.net_profit,
    small: group.below_comparison_floor,
  }));

  const columns: Column<Row>[] = [
    { key: "key", header: "Group", render: (r) => <span className="font-mono">{r.key}</span> },
    {
      key: "trades",
      header: "n",
      align: "right",
      render: (r) => (
        <span className={r.small ? "text-warning" : undefined} title={r.small ? "low sample" : undefined}>
          {r.trades}
        </span>
      ),
    },
    { key: "win_rate", header: "Win rate", align: "right", render: (r) => pct(r.win_rate) ?? "N/A" },
    { key: "net_profit", header: "Net", align: "right", render: (r) => cell(r.net_profit) },
    { key: "profit_factor", header: "PF", align: "right", render: (r) => cell(r.profit_factor) },
    { key: "expectancy", header: "Expectancy", align: "right", render: (r) => cell(r.expectancy) },
    { key: "r_total", header: "Total R", align: "right", render: (r) => cell(r.r_total) },
  ];

  return (
    <Panel
      title="Breakdown"
      level={32}
      right={
        <Select aria-label="Dimension" value={dimension} onChange={(e) => onDimension(e.target.value)}>
          {DIMENSIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
      }
    >
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.key}
        loading={query.isLoading}
        error={query.isError ? "The breakdown could not be read." : null}
        empty="NO GROUPS"
        emptyHint="No completed trades match this filter."
      />
      <p className="mt-2 text-body text-muted">
        Sample size is the second column and an amber one is below the comparison floor. A
        difference between 43 trades and 11 is not a finding.
      </p>
    </Panel>
  );
}

function ExecutionQuality({ filters }: { filters: AnalyticsFilters }) {
  const query = useQuery({
    queryKey: ["analytics-execution", filters],
    queryFn: () => analyticsService.execution(filters),
  });
  const data = query.data;

  if (query.isLoading) return <LoadingState what="execution quality" />;
  if (!data) return null;

  return (
    <Panel title="Execution quality" level={32}>
      {data.orders === 0 ? (
        <EmptyState
          message="NO ORDERS"
          hint="Nothing was submitted in this window, so there is no fill ratio, latency or slippage to report."
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile label="Orders" value={String(data.orders)} />
            <StatTile label="Fill ratio" value={pct(data.fill_ratio)} />
            <StatTile label="Rejection ratio" value={pct(data.rejection_ratio)} />
            <StatTile label="Partial fills" value={String(data.partial_fills)} />
          </div>
          <p className="mt-2 text-body text-muted">{data.latency_seconds.measured_from}</p>
          <p className="mt-1 text-body text-muted">{data.slippage_note}</p>
        </>
      )}
    </Panel>
  );
}
