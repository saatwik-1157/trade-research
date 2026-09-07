"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import {
  modelMonitoringService,
  type ModelAlertRow,
  type ModelHealthRow,
  type ModelHealthState,
  type MonitoringSnapshotRow,
} from "@/lib/services";

/**
 * The AI monitoring center. §36 and §37.
 *
 * **INSUFFICIENT_DATA is not green and OFFLINE is not red.** The two states most
 * often got wrong get their own tone: `INSUFFICIENT_DATA` is neutral because
 * nothing was concluded, and `OFFLINE` is neutral because nothing is deployed —
 * a statement about the registry rather than about the model. Rendering either
 * as healthy would be the false clean bill of health this level exists to
 * prevent; rendering OFFLINE as critical would page somebody about a model
 * nobody is using.
 *
 * **Every number is shown with its sample size.** §13. A profit factor from
 * seven observations and one from seven hundred render identically otherwise,
 * and the table would be inviting the reader to treat them the same.
 *
 * **Charts would need real data.** §37 says charts must use real data and §59
 * forbids fake values for visual appearance. Nothing has run on real inference
 * here — `market_bars` is empty — so this renders measured tables rather than a
 * sparkline drawn from nothing.
 */
const HEALTH_TONE: Record<ModelHealthState, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  HEALTHY: "accent",
  WARNING: "warning",
  DEGRADED: "warning",
  CRITICAL: "critical",
  // Nothing was concluded. Not a pass, and not a fault either.
  INSUFFICIENT_DATA: "neutral",
  // Nothing is deployed. A registry fact, not a model fault.
  OFFLINE: "neutral",
};

const ALERT_TONE: Record<string, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  info: "neutral",
  warning: "warning",
  critical: "critical",
};

export function ModelHealth() {
  const query = useQuery({ queryKey: ["monitoring-health"], queryFn: modelMonitoringService.health });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ModelHealthRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ModelHealthRow>[] = [
    {
      key: "health",
      header: "Health",
      render: (r) => <Badge tone={HEALTH_TONE[r.health] ?? "neutral"}>{r.health}</Badge>,
    },
    {
      key: "model",
      header: "Model",
      render: (r) => (
        <span className="font-mono text-body">{`${r.model} v${r.version ?? "?"}`}</span>
      ),
    },
    { key: "environment", header: "Env", render: (r) => r.environment },
    { key: "scope", header: "Scope", render: (r) => <Scope row={r} /> },
    {
      key: "sample",
      header: "Sample",
      align: "right",
      render: (r) => <Sample n={r.sample_count} />,
    },
    { key: "measured", header: "Measured", render: (r) => <Measured row={r} /> },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.model_version_id + r.environment}
        loading={query.isPending}
        empty="nothing is deployed"
        emptyHint="Monitoring watches the deployments the registry says are active."
        caption="Model health"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        <strong>INSUFFICIENT_DATA is not a pass</strong> — it means the sample could not support a
        conclusion. <strong>OFFLINE is not a clean bill of health</strong> — it means nothing is
        deployed. A deployment that has never been monitored reports the first, never HEALTHY:
        never measured and measured-and-fine must not look the same.
      </p>
    </div>
  );
}

function Scope({ row }: { row: ModelHealthRow }) {
  const parts = [
    row.scope.strategy_key && `strategy=${row.scope.strategy_key}`,
    row.scope.symbol && `symbol=${row.scope.symbol}`,
    row.scope.timeframe && `tf=${row.scope.timeframe}`,
  ].filter(Boolean);
  return (
    <span className="font-mono text-body">{parts.length ? parts.join(" ") : "unrestricted"}</span>
  );
}

/** Always visible. §13: a figure without its n invites being read as evidence. */
function Sample({ n }: { n: number }) {
  return (
    <span className={n > 0 ? "font-mono text-body" : "font-mono text-body text-muted"}>
      {n}
    </span>
  );
}

function Measured({ row }: { row: ModelHealthRow }) {
  if (!row.measured_at) {
    return <span className="text-body text-muted">never</span>;
  }
  return <span className="text-body">{row.measured_at.slice(0, 19)}</span>;
}

export function ModelAlerts() {
  const query = useQuery({ queryKey: ["monitoring-alerts"], queryFn: modelMonitoringService.alerts });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: ModelAlertRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<ModelAlertRow>[] = [
    {
      key: "severity",
      header: "Severity",
      render: (r) => <Badge tone={ALERT_TONE[r.severity] ?? "neutral"}>{r.severity}</Badge>,
    },
    { key: "status", header: "Status", render: (r) => <Status row={r} /> },
    { key: "check", header: "Check", render: (r) => `${r.check} · ${r.subject}` },
    { key: "value", header: "Value", align: "right", render: (r) => <Value row={r} /> },
    {
      key: "sample",
      header: "Sample",
      align: "right",
      render: (r) => <Sample n={r.sample_size ?? 0} />,
    },
    { key: "seen", header: "Seen", align: "right", render: (r) => r.occurrences },
    { key: "since", header: "Since", render: (r) => r.first_seen_at?.slice(0, 19) ?? "—" },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no alerts"
        emptyHint="An alert is one row per condition, not one per observation."
        caption="Monitoring alerts"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        One row per <em>condition</em>, not per observation: a condition already open and inside its
        cooldown is suppressed rather than repeated, and one that cleared is resolved with the time
        it cleared. <strong>An alert is a message.</strong> Monitoring never retrains, promotes,
        replaces or deploys a model, and never changes a risk limit or a position size.
      </p>
    </div>
  );
}

/** A resolved alert keeps its row. §25: the history should read raised → cleared. */
function Status({ row }: { row: ModelAlertRow }) {
  if (row.status === "resolved") {
    return (
      <span className="text-body text-muted">
        {`resolved ${row.resolved_at?.slice(11, 19) ?? ""}`.trim()}
      </span>
    );
  }
  return <span className="text-body">{row.status}</span>;
}

/** The value beside the threshold it crossed. §22. */
function Value({ row }: { row: ModelAlertRow }) {
  if (row.current_value === null) return <span className="text-muted">—</span>;
  const threshold = row.threshold === null ? "" : ` / ${row.threshold}`;
  return <span className="font-mono text-body">{`${row.current_value}${threshold}`}</span>;
}

export function MonitoringSnapshots() {
  const query = useQuery({
    queryKey: ["monitoring-snapshots"],
    queryFn: modelMonitoringService.snapshots,
  });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: MonitoringSnapshotRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<MonitoringSnapshotRow>[] = [
    {
      key: "health",
      header: "Health",
      render: (r) => <Badge tone={HEALTH_TONE[r.health] ?? "neutral"}>{r.health}</Badge>,
    },
    {
      key: "model",
      header: "Model",
      render: (r) => (
        <span className="font-mono text-body">{`${r.model} v${r.version ?? "?"}`}</span>
      ),
    },
    { key: "baseline", header: "Baseline", render: (r) => <BaselineCell row={r} /> },
    {
      key: "sample",
      header: "Sample",
      align: "right",
      render: (r) => <Sample n={r.sample_count} />,
    },
    { key: "blocks", header: "Measured", render: (r) => <Blocks row={r} /> },
    { key: "at", header: "At", render: (r) => r.created_at?.slice(0, 19) ?? "—" },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no snapshots"
        emptyHint="A snapshot is written each time monitoring runs over a deployment."
        caption="Monitoring snapshots"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        Each snapshot records the exact window it covered and the <strong>baseline</strong> it was
        measured against, so two snapshots computed against different references are visibly
        different rather than quietly incomparable. A baseline is never changed silently.
      </p>
    </div>
  );
}

function BaselineCell({ row }: { row: MonitoringSnapshotRow }) {
  if (!row.baseline.kind) {
    return <span className="text-body text-muted">none available</span>;
  }
  return (
    <span className="font-mono text-body" title={row.baseline.id ?? ""}>
      {row.baseline.kind}
    </span>
  );
}

/** Which metric families were actually computed. A NULL block is not a zero. */
function Blocks({ row }: { row: MonitoringSnapshotRow }) {
  const names = Object.entries(row.metrics)
    .filter(([, block]) => block !== null)
    .map(([name]) => name);
  if (!names.length) return <span className="text-body text-muted">nothing</span>;
  return <span className="font-mono text-body">{names.join(", ")}</span>;
}
