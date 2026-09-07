"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, DataTable, type Column, Unavailable } from "@/components/ui";
import { datasetService, type DatasetRow, type DatasetStatus } from "@/lib/services";

/**
 * Datasets, and whether validation let them through.
 *
 * **The leakage column is the one that matters.** A dataset with good coverage,
 * a high quality score and a failing leakage check is worse than no dataset at
 * all: every metric a model trained on it produces would look better than the
 * truth, and nothing downstream would say why. So it is rendered as an explicit
 * ✗ with the reason on hover rather than as a status a reader has to interpret.
 *
 * Nothing here can change a status. READY is what the builder concluded, and
 * the backend has no route that overrides it.
 */
const TONE: Record<DatasetStatus, "good" | "warning" | "critical"> = {
  RAW: "critical",
  CLEAN: "warning",
  READY: "good",
};

export function DatasetTable() {
  const query = useQuery({ queryKey: ["datasets"], queryFn: datasetService.list });
  const unavailable = query.data && !query.data.available ? query.data : null;
  const rows: DatasetRow[] = query.data?.available ? query.data.data : [];

  const columns: Column<DatasetRow>[] = [
    { key: "key", header: "Dataset", render: (r) => `${r.key} v${r.version}` },
    {
      key: "status",
      header: "Status",
      render: (r) => <Badge tone={TONE[r.status] ?? "neutral"}>{r.status}</Badge>,
    },
    { key: "leakage", header: "Leakage", render: (r) => <Leakage row={r} /> },
    {
      key: "series",
      header: "Series",
      render: (r) => (
        <span className="font-mono text-micro text-ink-2">
          {r.provider} · {r.timeframe}
        </span>
      ),
    },
    { key: "rows", header: "Rows", align: "right", render: (r) => r.rows.toLocaleString() },
    { key: "range", header: "Range", render: (r) => <Range row={r} /> },
    {
      key: "quality",
      header: "Quality",
      align: "right",
      render: (r) => (r.quality_score === null ? "—" : `${r.quality_score}/100`),
    },
    {
      key: "versions",
      header: "Feature / label",
      render: (r) => (
        <span className="font-mono text-micro text-ink-2">
          f{r.feature_set_version ?? "?"} · l{r.label_set_version ?? "?"}
        </span>
      ),
    },
    { key: "fingerprint", header: "Fingerprint", render: (r) => <Fingerprint row={r} /> },
  ];

  return (
    <div className="flex flex-col gap-3">
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        loading={query.isPending}
        empty="no datasets built"
        emptyHint="A dataset is built from stored bars; ingest history first."
        caption="Datasets"
      />
      {unavailable && (
        <Unavailable level={unavailable.level} reason={unavailable.reason} today={unavailable.today} />
      )}
      <p className="text-body leading-relaxed text-muted">
        READY means every leakage check passed. A dataset that failed one stays at CLEAN with the
        failure attached, and no caller — including this page — can override that.
      </p>
    </div>
  );
}

/** Passed, failed, or never run. The third is not the same as the first. */
function Leakage({ row }: { row: DatasetRow }) {
  if (row.leakage_passed === null) {
    return (
      <span className="text-muted" title="validation has not run against this dataset">
        not run
      </span>
    );
  }
  if (!row.leakage_passed) {
    return (
      <span className="font-mono text-critical" title={row.blocked_reason ?? undefined}>
        {row.leakage_failed_checks ?? "?"} failed ✗
      </span>
    );
  }
  return <span className="font-mono text-ink-2">passed</span>;
}

function Range({ row }: { row: DatasetRow }) {
  if (!row.start || !row.end) return <span className="text-muted">—</span>;
  return (
    <span className="font-mono text-micro text-ink-2">
      {row.start.slice(0, 10)} → {row.end.slice(0, 10)}
    </span>
  );
}

/**
 * A dataset with no fingerprint does not reproduce, and a blocked one has
 * none deliberately — a hash on a row whose rows were never built would read
 * as "this reproduces", which is exactly what it does not do.
 */
function Fingerprint({ row }: { row: DatasetRow }) {
  if (!row.fingerprint) {
    return (
      <span className="text-muted" title="nothing was built, so nothing reproduces">
        —
      </span>
    );
  }
  return (
    <span className="font-mono text-micro text-ink-2" title={row.fingerprint}>
      {row.fingerprint.slice(0, 12)}
    </span>
  );
}
